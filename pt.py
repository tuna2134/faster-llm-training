from dataclasses import dataclass
from transformers import TextIteratorStreamer

import torch
from torch import nn
import time
import torch.nn.functional as F

torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_math_sdp(True)
torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cudnn.allow_tf32 = True
torch.backends.cudnn.enabled = False
torch.set_float32_matmul_precision("high")


class Rotary(torch.nn.Module):

    def __init__(self, dim, base=10000):
        super().__init__()
        self.inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2).float() / dim))
        self.seq_len_cached = None
        self.cos_cached = None
        self.sin_cached = None

    def forward(self, x):
        seq_len = x.shape[1]
        if seq_len != self.seq_len_cached:
            self.seq_len_cached = seq_len
            t = torch.arange(seq_len, device=x.device).type_as(self.inv_freq)
            freqs = torch.outer(t, self.inv_freq).to(x.device)
            self.cos_cached = freqs.cos().bfloat16()
            self.sin_cached = freqs.sin().bfloat16()
        return self.cos_cached[None, :, None, :], self.sin_cached[None, :, None, :]


def apply_rotary_emb(x, cos, sin):
    assert x.ndim == 4  # multihead attention
    d = x.shape[3] // 2
    x1 = x[..., :d]
    x2 = x[..., d:]
    y1 = x1 * cos + x2 * sin
    y2 = x1 * (-sin) + x2 * cos
    return torch.cat([y1, y2], 3).type_as(x)


class CausalSelfAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.head_dim = self.n_embd // self.n_head
        assert self.n_embd % self.n_head == 0
        self.c_q = nn.Linear(self.n_embd, self.n_embd, bias=False)
        self.c_k = nn.Linear(self.n_embd, self.n_embd, bias=False)
        self.c_v = nn.Linear(self.n_embd, self.n_embd, bias=False)
        # output projection
        self.c_proj = nn.Linear(self.n_embd, self.n_embd, bias=False)
        self.c_proj.weight.data.zero_()  # zero init suggested by @Grad62304977
        self.rotary = Rotary(self.head_dim)
        self.lamb = nn.Parameter(torch.tensor(0.5))  # @Grad62304977

    def forward(self, x, v1=None):
        B, T, C = (
            x.size()
        )  # batch size, sequence length, embedding dimensionality (n_embd)
        q = self.c_q(x).view(B, T, self.n_head, self.head_dim)
        k = self.c_k(x).view(B, T, self.n_head, self.head_dim)
        v = self.c_v(x).view(B, T, self.n_head, self.head_dim)
        if v1 is None:
            v1 = v  # This happens if we are in the first block. v needs to be accessed by subsequent blocks
        v = (1 - self.lamb) * v + self.lamb * v1.view_as(v)  # @Grad62304977
        cos, sin = self.rotary(q)
        q, k = F.rms_norm(q, (q.size(-1),)), F.rms_norm(
            k, (k.size(-1),)
        )  # QK norm suggested by @Grad62304977
        q, k = apply_rotary_emb(q, cos, sin), apply_rotary_emb(k, cos, sin)
        y = F.scaled_dot_product_attention(
            q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), is_causal=True
        )
        y = (
            y.transpose(1, 2).contiguous().view_as(x)
        )  # re-assemble all head outputs side by side
        y = self.c_proj(y)
        return y, v1


class MLP(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd, bias=False)
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd, bias=False)
        self.c_proj.weight.data.zero_()  # zero init suggested by @Grad62304977

    def forward(self, x):
        x = self.c_fc(x)
        x = F.relu(
            x
        ).square()  # https://arxiv.org/abs/2109.08668v2; ~1-2% better than GELU; suggested by @SKYLINEZ007 and @Grad62304977
        x = self.c_proj(x)
        return x


class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.attn = CausalSelfAttention(config)
        self.mlp = MLP(config)
        self.lambdas = nn.Parameter(torch.tensor([1.0, 0.0]))

    def forward(self, x, v1, x0):
        x = self.lambdas[0] * x + self.lambdas[1] * x0
        x1, v1 = self.attn(F.rms_norm(x, (x.size(-1),)), v1)
        x = x + x1
        x = x + self.mlp(F.rms_norm(x, (x.size(-1),)))
        return x, v1


# -----------------------------------------------------------------------------
# The main GPT-2 model


@dataclass
class GPTConfig:
    vocab_size: int = 50304
    n_layer: int = 12
    n_head: int = 6  # head dim 128 suggested by @Grad62304977
    n_embd: int = 768


class GPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(
            dict(
                wte=nn.Embedding(config.vocab_size, config.n_embd),
                h=nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            )
        )

        # U-net design by @brendanh0gan
        self.encoder_layers = config.n_layer // 2  # Half of the layers for encoder
        self.decoder_layers = (
            config.n_layer - self.encoder_layers
        )  # Remaining for decoder
        # Add learnable skip connection weights for decoder layers
        self.skip_weights = nn.Parameter(torch.ones(self.decoder_layers))

        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.lm_head.weight.data.zero_()  # @Grad62304977

    def forward(self, idx, target=None):

        # forward the GPT model itself
        x = self.transformer.wte(idx)  # token embeddings of shape (b, t, n_embd)
        x = F.rms_norm(x, (x.size(-1),))  # @Grad62304977
        x0 = x
        v1 = None

        # Store outputs for U-Net skip connections
        skip_connections = []

        # Encoder pass - process only the first half of the blocks
        for i in range(self.encoder_layers):
            x, v1 = self.transformer.h[i](x, v1, x0)
            skip_connections.append(x)  # Store the output for skip connections

        # Decoder pass - process the remaining blocks with weighted skip connections
        for i in range(self.decoder_layers):
            skip_connection = (
                skip_connections.pop()
            )  # Get the corresponding encoder output
            # Apply learnable weight to skip connection
            weighted_skip = self.skip_weights[i] * skip_connection
            x, v1 = self.transformer.h[self.encoder_layers + i](
                x + weighted_skip, v1, x0
            )

        x = F.rms_norm(x, (x.size(-1),))
        if target is not None:
            logits = self.lm_head(x)
            logits = 30 * torch.tanh(logits / 30)  # @Grad62304977
            logits = logits.float()
            loss = F.cross_entropy(
                logits[:, :-1].reshape(-1, logits.size(-1)),
                target[:, 1:].reshape(-1),
            )
        else:
            loss = None
            logits = self.lm_head(x)
            logits = 30 * torch.tanh(logits / 30)  # @Grad62304977
            logits = logits.float()
        return logits, loss

    @torch.inference_mode()
    def generate(
        self,
        idx: torch.Tensor,
        max_new_tokens: int,
        temperature=1.0,
        top_k=None,
        repeat_penalty: float = 1.0,
        eos: int = -1,
        streamer: TextIteratorStreamer = None,
    ):
        for _ in range(max_new_tokens):

            # get predictions
            logits, _ = self(idx)

            # focus only on the last time step
            logits = logits[:, -1, :] / temperature
            if repeat_penalty > 1.0:
                # get unique tokens in the context
                context_tokens = idx[0].unique()

                # create penalty tensor (1.0 for unseen tokens, repeat_penalty for seen tokens)
                penalty = torch.ones_like(logits)
                penalty[:, context_tokens] = repeat_penalty
                # apply penalty by dividing logits
                logits = logits / penalty
            # optionally crop probabilities to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = float("-inf")

            # apply softmax to convert logits to probabilities
            probs = F.softmax(logits, dim=-1)

            # sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)
            # append sampled index to the running sequence
            idx = torch.cat((idx, idx_next), dim=1)
            if streamer:
                streamer.put(idx_next)
            if idx_next == eos:
                break
        if streamer:
            streamer.end()
        return idx


device = "cuda"
gpt = GPT(GPTConfig()).to(device).bfloat16()
gpt = torch.compile(gpt)
optim = torch.optim.AdamW(gpt.parameters(), 1e-3)
logits, loss = gpt(
    torch.tensor([[1, 2, 3]], dtype=torch.long).to(device),
    torch.tensor([[1, 2, 3]], dtype=torch.long).to(device),
)
now = time.time()

for i in range(100):
    logits, loss = gpt(
        torch.tensor([[1, 2, 3]], dtype=torch.long).to(device),
        torch.tensor([[1, 2, 3]], dtype=torch.long).to(device),
    )
    loss.backward()
    optim.step()
    print(f"Step:{i}/Loss:{loss.item()}/Time:{time.time()-now}")
