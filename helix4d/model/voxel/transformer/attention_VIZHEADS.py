import torch
from torch import nn
import numpy as np
import torch_scatter
import torch.nn.functional as F

from .utils import _init_weights

@torch.jit.script
def compute_relative_posenc_mul(relative_positional_encoding_nowd, buckets):
    return torch.index_select(relative_positional_encoding_nowd, 0, buckets)

@torch.jit.script
def compute_relative_posenc_plus(relative_positional_encoding_nowd, buckets):
    _, dim_buckets = buckets.size()
    _, n_heads, dim = relative_positional_encoding_nowd.size()
    posenc_all_dims = torch.index_select(relative_positional_encoding_nowd, 0, buckets.flatten())
    return posenc_all_dims.reshape(-1, dim_buckets, n_heads, dim).sum(1)

class Attention(torch.jit.ScriptModule):

    def __init__(self, dim_in, transformer):
        super().__init__()

        self.dim_in = dim_in
        self.d = dim_in // transformer.n_heads
        self.h = transformer.n_heads

        self.d_qk = transformer.dim_qk if transformer.dim_qk != 0 else dim_in // transformer.n_heads

        self.normalize_qk = transformer.temperature_qk > 0
        if self.normalize_qk:
            self.temperature_nowd = nn.Parameter(
                transformer.temperature_qk*torch.ones(self.h))
        else:
            self.register_buffer("temperature_nowd",
                                 1/(self.d_qk**.5)*torch.ones(self.h))

        self.samequerykey = transformer.samequerykey
        self.maxi_d = self.d + (2 - self.samequerykey)*self.d_qk

        self.qkv_linear = nn.Linear(
            dim_in, self.maxi_d*self.h, bias=False)

        if transformer.do_o:
            self.o_linear = nn.Linear(dim_in, dim_in)

        self.drop_proba = nn.Dropout(transformer.dropout)
        self.drop_out = nn.Dropout(transformer.dropout)

        self.do_relative_positional = transformer.do_relative_positional

        self.relative_positional_mode = transformer.relative_positional_mode

        if self.relative_positional_mode == "mul":
            n_buckets = 2*torch.tensor(transformer.relative_positional_beta)+1
            n_buckets = torch.prod(n_buckets).item()
            n_buckets *= transformer.relative_positional_ntime

            self.relative_positional_encoding_nowd = nn.Parameter(
                torch.zeros((n_buckets, self.h, self.d_qk)))

            self.compute_relative_posenc = compute_relative_posenc_mul

        elif self.relative_positional_mode == "plus":
            n_buckets = list(2*np.array(transformer.relative_positional_beta)+1)

            if transformer.relative_positional_ntime != 1:
                n_buckets += [transformer.relative_positional_ntime]

            self.relative_positional_encoding_nowd = nn.Parameter(torch.zeros((np.sum(n_buckets), self.h, self.d_qk)))

            self.compute_relative_posenc = compute_relative_posenc_plus
        else:
            raise ValueError

        self.apply(_init_weights)

    @torch.jit.script_method
    def forward(self, x, iq, ik, buckets, keystouse, valuestouse):
        with torch.profiler.record_function("ATT"):

            query_key_value = self.qkv_linear(x).view(-1, self.h, self.maxi_d)

            if self.samequerykey:
                query = key = query_key_value[..., :self.d_qk]
            else:
                query = query_key_value[..., :self.d_qk]
                key = query_key_value[..., self.d_qk:2*self.d_qk]
            value = query_key_value[..., -self.d:]
            proba, key = self.compute_proba(
                query, key, iq, ik, buckets, keystouse=keystouse)

            value_ik = torch.index_select(
                torch.cat([value, valuestouse], 0), 0, ik)

            out = torch_scatter.scatter_add(
                self.drop_proba(proba).unsqueeze(-1) * value_ik, iq, 0).flatten(-2, -1)

            if hasattr(self, "o_linear"):
                out = self.o_linear(out)

            return self.drop_out(out), proba, key, value

    @torch.jit.script_method
    def get_T(self):
        return self.temperature_nowd.view(1, -1)

    def do_plot(self, C, b):
        import matplotlib
        import matplotlib.pyplot as plt
        from matplotlib import rc
        #rc('font',**{'family':'sans-serif','sans-serif':['Helvetica']})
        ## for Palatino and other serif fonts use:
        #rc('font',**{'family':'serif','serif':['Palatino']})
        #rc('text', usetex=True)

        fig, axs = plt.subplots(len(C), self.h, figsize=(10,2.5*len(C)),
            sharex="col", sharey="row")

        for row, (c, clabel) in enumerate(C):
            for i, dim in enumerate(["X", "Y", "Z", "T"]):
                BX = torch_scatter.scatter_mean(c, b[:, i] - 7*i, 0)
                #plt.subplot(len(C), self.h, 1+i + self.h*row)

                
                
                if i <= 3:
                    DX = np.arange(1+b[:, i].max().item() - b[:, i].min().item())
                    DX -= int(np.mean(DX))
                else:
                    DX = np.arange(1+b[:, i].max().item() - b[:, i].min().item())

                if row == 0:
                    print(dim)
                    for bin in range(len(DX)):
                        thislist = [str(bin)] + list(BX[bin].detach().cpu().numpy().astype(str))
                        print('\t'.join(thislist))
                        #print(f"{bin}\t{BX[bin].detach().cpu().numpy().astype(str).join('\t')}")




                axs[row, i].plot(DX, BX.detach().cpu().numpy(),
                label=["h=1", "h=2", "h=3", "h=4"]if i+row ==0 else None)
                if i+row ==0:axs[row, i].legend(loc="best")
                if row==len(C)-1:axs[row, i].set_xlabel(f"Delta {dim} (bin)")
                if i==0:axs[row, i].set_ylabel(clabel)
                if row==0:axs[row, i].set_title(f"{dim}")
        plt.tight_layout()
        plt.show()

    @torch.jit.script_method
    def compute_proba(self, query, key, iq, ik, buckets, keystouse):
        with torch.profiler.record_function("PROBAS"):

            if self.normalize_qk:
                # Normalizing keys and queries
                query = F.normalize(query, dim=-1)
                key = F.normalize(key, dim=-1)

            query_iq = torch.index_select(query, 0, iq)
            oqk = (query_iq * torch.index_select(torch.cat([key, keystouse], 0), 0, ik)).sum(-1)

            if self.do_relative_positional:
                with torch.profiler.record_function("POSENC"):
                    posenc = (query_iq * self.compute_relative_posenc(self.relative_positional_encoding_nowd, buckets)).sum(-1)
                    qk = oqk + posenc

            qk = qk * self.get_T()

            with torch.profiler.record_function("NORM PROBA"):
                proba_qk = torch_scatter.scatter_softmax(qk, iq, 0)

            self.do_plot([
                (posenc, "k_v^h . PE^h"),
                (oqk, "k_v^h . k_u^h"),
                (qk, "y_{u,v}^h"),
                (proba_qk, "a_{u,v}^h")
            ], buckets)


            print("")
            return proba_qk, key