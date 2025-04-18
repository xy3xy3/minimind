import math
import struct
import inspect
import time

from .LMConfig import LMConfig
from typing import Any, Optional, Tuple, List, Union
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from transformers import PreTrainedModel
from transformers.modeling_outputs import CausalLMOutputWithPast


class RMSNorm(torch.nn.Module):
    """
    均方根层归一化（Root Mean Square Layer Normalization）
    相比传统的 LayerNorm，RMSNorm 计算更简单，不需要减去均值，
    只需要对均方根进行归一化，计算效率更高
    """
    def __init__(self, dim: int, eps: float = 1e-6):
        """
        初始化 RMSNorm 层

        参数:
            dim: 需要归一化的特征维度
            eps: 添加到分母中的小常数，防止除零错误
        """
        super().__init__()
        self.eps = eps
        # 可学习的缩放参数
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        """
        执行 RMS 归一化计算

        计算公式: x / sqrt(mean(x^2) + eps)
        """
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        """
        前向传播函数

        先将输入转为 float 类型进行归一化计算，然后再转回原始数据类型
        最后乘以可学习的权重参数
        """
        return self.weight * self._norm(x.float()).type_as(x)


def precompute_pos_cis(dim: int, end: int = int(32 * 1024), theta: float = 1e6):
    """
    预计算旋转位置编码（Rotary Position Embeddings, RoPE）所需的复数值

    参数:
        dim: 隐藏维度大小
        end: 最大序列长度，默认为32K
        theta: RoPE中的缩放因子，影响位置编码的频率

    返回:
        pos_cis: 预计算好的复数形式的位置编码，形状为[end, dim//2]
    """
    # 计算不同频率的逆频率项
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    # 生成位置索引
    t = torch.arange(end, device=freqs.device)  # type: ignore
    # 计算外积得到每个位置对应的每个频率
    freqs = torch.outer(t, freqs).float()  # type: ignore
    # 使用欧拉公式 e^(i*θ) = cos(θ) + i*sin(θ) 生成复数
    # 幅值为1，相位为freqs的复数值
    pos_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return pos_cis


def apply_rotary_emb(xq, xk, pos_cis):
    """
    将旋转位置编码应用到查询(Q)和键(K)张量上

    参数:
        xq: 查询张量, 形状为[batch_size, seq_len, n_heads, head_dim]
        xk: 键张量, 形状为[batch_size, seq_len, n_kv_heads, head_dim]
        pos_cis: 预计算的位置编码复数

    返回:
        应用位置编码后的查询和键张量
    """
    def unite_shape(pos_cis, x):
        """
        调整pos_cis的形状使其与输入张量x兼容，便于广播计算
        """
        ndim = x.ndim
        assert 0 <= 1 < ndim
        assert pos_cis.shape == (x.shape[1], x.shape[-1])
        # 创建一个新形状，只保留序列长度和特征维度，其余维度设为1
        shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
        return pos_cis.view(*shape)

    # 将Q和K重塑并转换为复数形式
    # 将最后一个维度每两个相邻元素视为一个复数的实部和虚部
    xq_ = torch.view_as_complex(xq.float().reshape(*xq.shape[:-1], -1, 2))
    xk_ = torch.view_as_complex(xk.float().reshape(*xk.shape[:-1], -1, 2))

    # 调整pos_cis的形状以便与输入张量兼容
    pos_cis = unite_shape(pos_cis, xq_)

    # 应用旋转操作：在复数域中，乘以pos_cis等同于旋转
    xq_out = torch.view_as_real(xq_ * pos_cis).flatten(3)
    xk_out = torch.view_as_real(xk_ * pos_cis).flatten(3)

    # 转换回输入张量的原始数据类型
    return xq_out.type_as(xq), xk_out.type_as(xk)


def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    实现键值张量的重复操作，用于注意力机制中的多头处理

    功能等同于torch.repeat_interleave(x, dim=2, repeats=n_rep)

    用于将KV头扩展至与Q头数量匹配（当KV头少于Q头时）

    参数:
        x: 输入张量，形状为[batch_size, seq_len, n_kv_heads, head_dim]
        n_rep: 每个KV头重复的次数

    返回:
        重复后的张量，形状为[batch_size, seq_len, n_kv_heads*n_rep, head_dim]
    """
    bs, slen, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    # 在第三维插入新维度，然后扩展，最后重塑回原来的维度结构
    return (
        x[:, :, :, None, :]  # 插入新维度: [bs, slen, n_kv_heads, 1, head_dim]
        .expand(bs, slen, n_kv_heads, n_rep, head_dim)  # 扩展维度: [bs, slen, n_kv_heads, n_rep, head_dim]
        .reshape(bs, slen, n_kv_heads * n_rep, head_dim)  # 重塑: [bs, slen, n_kv_heads*n_rep, head_dim]
    )


class Attention(nn.Module):
    """
    多头注意力机制（Multi-Head Attention）实现

    注意力机制是Transformer架构的核心组件，允许模型关注输入序列中的不同部分。
    本实现支持：
    1. 分组查询注意力（Grouped-Query Attention, GQA）：允许Q头数量多于KV头数量
    2. 旋转位置编码（Rotary Position Embedding, RoPE）：通过复数旋转编码位置信息
    3. 注意力掩码：确保模型只能看到当前及之前的token（因果关系）
    4. KV缓存：用于加速自回归生成过程
    5. Flash Attention：当PyTorch版本支持时，使用更高效的注意力计算

    计算流程：
    1. 将输入通过线性层投影为查询(Q)、键(K)和值(V)
    2. 应用旋转位置编码
    3. 计算注意力分数：Q与K的点积，并进行缩放
    4. 应用因果掩码确保自回归属性
    5. 对分数进行softmax归一化
    6. 将注意力权重与V相乘得到输出
    7. 通过输出投影层转换回原始维度
    """
    def __init__(self, args: LMConfig):
        """
        初始化注意力层

        参数:
            args: 模型配置参数
        """
        super().__init__()
        # 确定KV头的数量，如果未指定则与Q头数量相同
        self.n_kv_heads = args.n_heads if args.n_kv_heads is None else args.n_kv_heads
        # 确保Q头数量是KV头数量的整数倍，这是GQA的要求
        assert args.n_heads % self.n_kv_heads == 0
        # 查询(Q)头的数量
        self.n_local_heads = args.n_heads
        # 键值(KV)头的数量
        self.n_local_kv_heads = self.n_kv_heads
        # 每个KV头对应的Q头数量，用于GQA中的头部复制
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        # 每个注意力头的维度
        self.head_dim = args.dim // args.n_heads
        # Q、K、V的线性投影层
        self.wq = nn.Linear(args.dim, args.n_heads * self.head_dim, bias=False)
        self.wk = nn.Linear(args.dim, self.n_kv_heads * self.head_dim, bias=False)
        self.wv = nn.Linear(args.dim, self.n_kv_heads * self.head_dim, bias=False)
        # 输出投影层，将多头注意力的结果映射回模型维度
        self.wo = nn.Linear(args.n_heads * self.head_dim, args.dim, bias=False)
        # 注意力权重的dropout
        self.attn_dropout = nn.Dropout(args.dropout)
        # 残差连接的dropout
        self.resid_dropout = nn.Dropout(args.dropout)
        self.dropout = args.dropout
        # 检测是否可以使用Flash Attention（需要PyTorch >= 2.0）
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention') and args.flash_attn
        # print("WARNING: using slow attention. Flash Attention requires PyTorch >= 2.0")
        # 创建因果掩码（上三角矩阵，对角线以上为-inf）
        # 这确保了每个位置只能关注自身及之前的位置，实现自回归属性
        mask = torch.full((1, 1, args.max_seq_len, args.max_seq_len), float("-inf"))
        mask = torch.triu(mask, diagonal=1)
        self.register_buffer("mask", mask, persistent=False)

    def forward(self,
                x: torch.Tensor,
                pos_cis: torch.Tensor,
                past_key_value: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
                use_cache=False):
        """
        注意力层的前向传播

        参数:
            x: 输入张量，形状为 [batch_size, seq_len, hidden_dim]
            pos_cis: 预计算的旋转位置编码
            past_key_value: 可选的KV缓存，用于加速自回归生成
            use_cache: 是否使用并返回KV缓存

        返回:
            output: 注意力层的输出，形状为 [batch_size, seq_len, hidden_dim]
            past_kv: 更新后的KV缓存（如果use_cache=True）
        """
        # 获取输入张量的形状信息
        bsz, seq_len, _ = x.shape

        # 1. 线性投影：将输入投影到查询(Q)、键(K)和值(V)空间
        xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)

        # 2. 重塑张量以便多头处理
        # 从 [batch_size, seq_len, heads*head_dim] 变为 [batch_size, seq_len, heads, head_dim]
        xq = xq.view(bsz, seq_len, self.n_local_heads, self.head_dim)
        xk = xk.view(bsz, seq_len, self.n_local_kv_heads, self.head_dim)
        xv = xv.view(bsz, seq_len, self.n_local_kv_heads, self.head_dim)

        # 3. 应用旋转位置编码(RoPE)到查询和键
        xq, xk = apply_rotary_emb(xq, xk, pos_cis)

        # 4. KV缓存处理：如果有历史KV，则与当前KV拼接
        # 这在生成时很有用，避免重复计算已处理token的KV值
        if past_key_value is not None:
            xk = torch.cat([past_key_value[0], xk], dim=1)  # 拼接历史K和当前K
            xv = torch.cat([past_key_value[1], xv], dim=1)  # 拼接历史V和当前V
        # 如果需要缓存，则保存当前KV用于下一步
        past_kv = (xk, xv) if use_cache else None

        # 5. 张量变换准备计算注意力
        # - 转置维度，使头维度在前，便于批量计算
        # - 对KV进行重复，实现分组查询注意力(GQA)
        xq, xk, xv = (
            xq.transpose(1, 2),  # [batch_size, n_heads, seq_len, head_dim]
            repeat_kv(xk, self.n_rep).transpose(1, 2),  # 重复KV头以匹配Q头数量
            repeat_kv(xv, self.n_rep).transpose(1, 2)
        )

        # 6. 注意力计算
        if self.flash and seq_len != 1:  # 使用Flash Attention（如果可用且序列长度>1）
            dropout_p = self.dropout if self.training else 0.0
            output = F.scaled_dot_product_attention(
                xq, xk, xv,
                attn_mask=None,  # Flash Attention内部处理因果掩码
                dropout_p=dropout_p,
                is_causal=True  # 指示使用因果掩码
            )
        else:  # 使用传统注意力计算
            # 计算注意力分数：Q和K的矩阵乘法，然后除以缩放因子
            scores = (xq @ xk.transpose(-2, -1)) / math.sqrt(self.head_dim)
            # 应用因果掩码，确保只关注当前及之前的token
            scores += self.mask[:, :, :seq_len, :seq_len]
            # 对分数进行softmax归一化，得到注意力权重
            scores = F.softmax(scores.float(), dim=-1).type_as(xq)
            # 应用dropout
            scores = self.attn_dropout(scores)
            # 将注意力权重与V相乘得到加权值
            output = scores @ xv

        # 7. 重塑输出并通过输出投影层
        # 转置回原始维度顺序并合并多头结果
        output = output.transpose(1, 2).reshape(bsz, seq_len, -1)
        # 通过输出投影层并应用dropout
        output = self.resid_dropout(self.wo(output))

        return output, past_kv


class FeedForward(nn.Module):
    """
    前馈神经网络（Feed Forward Network, FFN）实现

    在Transformer架构中，FFN是注意力层之后的关键组件，用于增强模型的表达能力。
    本实现采用SwiGLU激活函数变体，相比传统FFN有更好的性能：

    传统FFN: FFN(x) = W₂·ReLU(W₁·x)
    SwiGLU: FFN(x) = W₂·(SiLU(W₁·x) * W₃·x)

    其中SiLU(x) = x·sigmoid(x)，也称为Swish激活函数

    这种设计有以下优点：
    1. 门控机制：W₃·x作为门控信号调节信息流
    2. 更平滑的激活函数：SiLU比ReLU更平滑，有利于优化
    3. 更强的表达能力：双路径设计增强了网络的表达能力
    """
    def __init__(self, config: LMConfig):
        """
        初始化前馈网络层

        参数:
            config: 模型配置参数
        """
        super().__init__()
        # 如果未指定隐藏层维度，则根据模型维度计算
        if config.hidden_dim is None:
            # 首先设置为模型维度的4倍
            hidden_dim = 4 * config.dim
            # 然后取2/3，这是SwiGLU变体的常用设置
            hidden_dim = int(2 * hidden_dim / 3)
            # 将隐藏维度调整为multiple_of的倍数，有助于硬件加速
            config.hidden_dim = config.multiple_of * ((hidden_dim + config.multiple_of - 1) // config.multiple_of)

        # 第一个投影层：将输入从模型维度映射到隐藏维度
        self.w1 = nn.Linear(config.dim, config.hidden_dim, bias=False)
        # 第二个投影层：将激活后的结果映射回模型维度
        self.w2 = nn.Linear(config.hidden_dim, config.dim, bias=False)
        # 第三个投影层：用于门控机制，与w1共同作用
        self.w3 = nn.Linear(config.dim, config.hidden_dim, bias=False)
        # dropout层，用于正则化
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        """
        前馈网络的前向传播

        实现公式: FFN(x) = dropout(W₂·(SiLU(W₁·x) * W₃·x))

        参数:
            x: 输入张量，形状为 [batch_size, seq_len, hidden_dim]

        返回:
            经过前馈网络处理后的张量，形状与输入相同
        """
        # SwiGLU激活函数变体：
        # 1. 计算W₁·x并应用SiLU激活函数
        # 2. 计算W₃·x作为门控信号
        # 3. 将两者相乘
        # 4. 通过W₂投影回原始维度
        # 5. 应用dropout
        return self.dropout(self.w2(F.silu(self.w1(x)) * self.w3(x)))


class MoEGate(nn.Module):
    def __init__(self, config: LMConfig):
        super().__init__()
        self.config = config
        self.top_k = config.num_experts_per_tok
        self.n_routed_experts = config.n_routed_experts

        self.scoring_func = config.scoring_func
        self.alpha = config.aux_loss_alpha
        self.seq_aux = config.seq_aux

        self.norm_topk_prob = config.norm_topk_prob
        self.gating_dim = config.dim
        self.weight = nn.Parameter(torch.empty((self.n_routed_experts, self.gating_dim)))
        self.reset_parameters()

    def reset_parameters(self) -> None:
        import torch.nn.init as init
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))

    def forward(self, hidden_states):
        bsz, seq_len, h = hidden_states.shape
        hidden_states = hidden_states.view(-1, h)
        logits = F.linear(hidden_states, self.weight, None)
        if self.scoring_func == 'softmax':
            scores = logits.softmax(dim=-1)
        else:
            raise NotImplementedError(f'insupportable scoring function for MoE gating: {self.scoring_func}')

        topk_weight, topk_idx = torch.topk(scores, k=self.top_k, dim=-1, sorted=False)

        if self.top_k > 1 and self.norm_topk_prob:
            denominator = topk_weight.sum(dim=-1, keepdim=True) + 1e-20
            topk_weight = topk_weight / denominator

        if self.training and self.alpha > 0.0:
            scores_for_aux = scores
            aux_topk = self.top_k
            topk_idx_for_aux_loss = topk_idx.view(bsz, -1)
            if self.seq_aux:
                scores_for_seq_aux = scores_for_aux.view(bsz, seq_len, -1)
                ce = torch.zeros(bsz, self.n_routed_experts, device=hidden_states.device)
                ce.scatter_add_(1, topk_idx_for_aux_loss,
                                torch.ones(bsz, seq_len * aux_topk, device=hidden_states.device)).div_(
                    seq_len * aux_topk / self.n_routed_experts)
                aux_loss = (ce * scores_for_seq_aux.mean(dim=1)).sum(dim=1).mean() * self.alpha
            else:
                mask_ce = F.one_hot(topk_idx_for_aux_loss.view(-1), num_classes=self.n_routed_experts)
                ce = mask_ce.float().mean(0)
                Pi = scores_for_aux.mean(0)
                fi = ce * self.n_routed_experts
                aux_loss = (Pi * fi).sum() * self.alpha
        else:
            aux_loss = 0
        return topk_idx, topk_weight, aux_loss


class MOEFeedForward(nn.Module):
    def __init__(self, config: LMConfig):
        super().__init__()
        self.config = config
        self.experts = nn.ModuleList([
            FeedForward(config)
            for _ in range(config.n_routed_experts)
        ])
        self.gate = MoEGate(config)
        if config.n_shared_experts is not None:
            self.shared_experts = FeedForward(config)

    def forward(self, x):
        identity = x
        orig_shape = x.shape
        bsz, seq_len, _ = x.shape
        # 使用门控机制选择专家
        topk_idx, topk_weight, aux_loss = self.gate(x)
        x = x.view(-1, x.shape[-1])
        flat_topk_idx = topk_idx.view(-1)
        if self.training:
            x = x.repeat_interleave(self.config.num_experts_per_tok, dim=0)
            y = torch.empty_like(x, dtype=torch.float16)
            for i, expert in enumerate(self.experts):
                y[flat_topk_idx == i] = expert(x[flat_topk_idx == i]).to(y.dtype)  # 确保类型一致
            y = (y.view(*topk_weight.shape, -1) * topk_weight.unsqueeze(-1)).sum(dim=1)
            y = y.view(*orig_shape)
        else:
            y = self.moe_infer(x, flat_topk_idx, topk_weight.view(-1, 1)).view(*orig_shape)
        if self.config.n_shared_experts is not None:
            y = y + self.shared_experts(identity)
        self.aux_loss = aux_loss
        return y

    @torch.no_grad()
    def moe_infer(self, x, flat_expert_indices, flat_expert_weights):
        expert_cache = torch.zeros_like(x)
        idxs = flat_expert_indices.argsort()
        tokens_per_expert = flat_expert_indices.bincount().cpu().numpy().cumsum(0)
        token_idxs = idxs // self.config.num_experts_per_tok
        # 当tokens_per_expert = [6, 15, 20, 26]，tokens_per_expert.shape[0]即为专家数量（此时为4）
        # 且token_idxs = [3, 7, 19, 21, 24, 25,  4,  5,  6, 10, 11, 12...] 时
        # 意味token_idxs[:6] -> [3, 7, 19, 21, 24, 25]这6个位置属于专家0处理的token（每个token有可能被多个专家处理，这取决于num_experts_per_tok）
        # 接下来9个位置token_idxs[6:15] -> [4,  5,  6, 10, 11, 12...]属于专家1处理的token...依此类推
        for i, end_idx in enumerate(tokens_per_expert):
            start_idx = 0 if i == 0 else tokens_per_expert[i - 1]
            if start_idx == end_idx:
                continue
            expert = self.experts[i]
            exp_token_idx = token_idxs[start_idx:end_idx]
            expert_tokens = x[exp_token_idx]
            expert_out = expert(expert_tokens).to(expert_cache.dtype)
            expert_out.mul_(flat_expert_weights[idxs[start_idx:end_idx]])
            expert_cache.scatter_add_(0, exp_token_idx.view(-1, 1).repeat(1, x.shape[-1]), expert_out)

        return expert_cache


class MiniMindBlock(nn.Module):
    """
    MiniMind模型的基本构建块

    这是Transformer架构的核心组件，每个块包含：
    1. 多头自注意力机制（Multi-Head Self-Attention）
    2. 前馈神经网络（Feed-Forward Network）

    每个组件前都有一个层归一化（Layer Normalization），并使用残差连接（Residual Connection）
    """
    def __init__(self, layer_id: int, config: LMConfig):
        """
        初始化MiniMind块

        参数:
            layer_id: 层的索引编号，用于区分不同层
            config: 模型配置参数
        """
        super().__init__()
        # 注意力头数量
        self.n_heads = config.n_heads
        # 模型隐藏维度
        self.dim = config.dim
        # 每个注意力头的维度
        self.head_dim = config.dim // config.n_heads
        # 注意力层
        self.attention = Attention(config)

        # 记录层索引，可用于位置相关的特殊处理
        self.layer_id = layer_id
        # 注意力层前的归一化
        self.attention_norm = RMSNorm(config.dim, eps=config.norm_eps)
        # 前馈网络前的归一化
        self.ffn_norm = RMSNorm(config.dim, eps=config.norm_eps)
        # 前馈网络层，根据配置选择普通FFN或混合专家FFN(MoE)
        self.feed_forward = FeedForward(config) if not config.use_moe else MOEFeedForward(config)

    def forward(self, x, pos_cis, past_key_value=None, use_cache=False):
        """
        前向传播函数

        参数:
            x: 输入张量，形状为 [batch_size, seq_len, hidden_dim]
            pos_cis: 旋转位置编码
            past_key_value: 可选的KV缓存，用于加速自回归生成
            use_cache: 是否使用并返回KV缓存

        返回:
            out: 经过处理后的输出张量
            past_kv: 更新后的KV缓存（如果use_cache=True）
        """
        # 1. 注意力子层：先归一化，再计算注意力，最后应用残差连接
        # 先对输入进行层归一化
        h_attn, past_kv = self.attention(
            self.attention_norm(x),
            pos_cis,
            past_key_value=past_key_value,
            use_cache=use_cache
        )
        # 应用第一个残差连接: x + Attention(LayerNorm(x))
        h = x + h_attn

        # 2. 前馈网络子层：先归一化，再通过前馈网络，最后应用残差连接
        # 应用第二个残差连接: h + FFN(LayerNorm(h))
        out = h + self.feed_forward(self.ffn_norm(h))
        return out, past_kv


class MiniMindLM(PreTrainedModel):
    """
    MiniMind语言模型主类

    这是基于Transformer架构的语言模型实现，结合了多种现代技术：
    1. 预归一化（Pre-Normalization）：在每个子层前而非后应用归一化
    2. 旋转位置编码（RoPE）：使用复数旋转来编码位置信息
    3. 参数共享：输入嵌入和输出层权重共享
    4. 混合专家模型（MoE）：可选的稀疏前馈网络实现

    模型整体结构：
    1. 词元嵌入层：将输入token ID转换为向量表示
    2. 多层Transformer块：每层包含自注意力和前馈网络
    3. 最终归一化层：对最后的隐藏状态进行归一化
    4. 输出层：将隐藏状态映射为词汇表大小的logits
    """
    config_class = LMConfig

    def __init__(self, params: LMConfig = None):
        """
        初始化MiniMind语言模型

        参数:
            params: 模型配置参数，包含维度、层数、头数等超参数
        """
        # 如果未提供配置，则使用默认配置
        self.params = params or LMConfig()
        super().__init__(self.params)

        # 设置基本参数
        self.vocab_size, self.n_layers = params.vocab_size, params.n_layers

        # 词元嵌入层：将token ID映射为向量表示
        self.tok_embeddings = nn.Embedding(params.vocab_size, params.dim)

        # Dropout层：用于正则化，防止过拟合
        self.dropout = nn.Dropout(params.dropout)

        # Transformer层：构建n_layers个MiniMindBlock
        self.layers = nn.ModuleList([MiniMindBlock(l, params) for l in range(self.n_layers)])

        # 最终归一化层：对最后的隐藏状态进行归一化
        self.norm = RMSNorm(params.dim, eps=params.norm_eps)

        # 输出层：将隐藏状态映射为词汇表大小的logits
        self.output = nn.Linear(params.dim, params.vocab_size, bias=False)

        # 参数共享：输入嵌入和输出层权重共享，减少参数量并提高性能
        self.tok_embeddings.weight = self.output.weight

        # 预计算位置编码：用于旋转位置编码(RoPE)
        self.register_buffer("pos_cis",
                             precompute_pos_cis(dim=params.dim // params.n_heads, theta=params.rope_theta),
                             persistent=False)

        # 输出容器：用于存储和返回模型的各种输出
        self.OUT = CausalLMOutputWithPast()

    def forward(self,
                input_ids: Optional[torch.Tensor] = None,
                past_key_values: Optional[List[Tuple[torch.Tensor, torch.Tensor]]] = None,
                use_cache: bool = False,
                logits_to_keep: Union[int, torch.Tensor] = 0,
                **args):
        """
        模型前向传播函数

        参数:
            input_ids: 输入的token ID，形状为[batch_size, seq_len]
            past_key_values: 可选的KV缓存，用于加速自回归生成
            use_cache: 是否使用并返回KV缓存
            logits_to_keep: 控制只计算部分位置的logits，可以是整数或张量
            **args: 其他参数，如start_pos（用于RoPE计算的起始位置）

        返回:
            CausalLMOutputWithPast对象，包含:
            - logits: 预测的下一个token概率分布
            - past_key_values: 更新后的KV缓存
            - last_hidden_state: 最后一层的隐藏状态
            - aux_loss: MoE模型的辅助损失（如果使用）
        """
        # 初始化KV缓存，如果未提供则为每层创建None
        past_key_values = past_key_values or [None] * len(self.layers)

        # 获取起始位置，用于RoPE计算，默认为0
        start_pos = args.get('start_pos', 0)

        # 1. 词元嵌入：将输入token ID转换为向量表示并应用dropout
        h = self.dropout(self.tok_embeddings(input_ids))

        # 2. 获取当前序列对应的位置编码
        pos_cis = self.pos_cis[start_pos:start_pos + input_ids.size(1)]

        # 3. 依次通过每个Transformer层
        past_kvs = []
        for l, layer in enumerate(self.layers):
            h, past_kv = layer(
                h, pos_cis,
                past_key_value=past_key_values[l],
                use_cache=use_cache
            )
            past_kvs.append(past_kv)

        # 4. 确定需要计算logits的位置索引
        # 如果logits_to_keep是整数，则只保留最后logits_to_keep个位置
        # 否则使用提供的张量作为索引
        slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep

        # 5. 计算输出logits：先归一化，然后通过输出层
        logits = self.output(self.norm(h)[:, slice_indices, :])

        # 6. 计算MoE辅助损失（如果使用MoE）
        aux_loss = sum(l.feed_forward.aux_loss for l in self.layers if isinstance(l.feed_forward, MOEFeedForward))

        # 7. 设置输出对象的各个字段
        self.OUT.__setitem__('last_hidden_state', h)  # 最后的隐藏状态
        self.OUT.__setitem__('logits', logits)  # 预测的logits
        self.OUT.__setitem__('aux_loss', aux_loss)  # MoE辅助损失
        self.OUT.__setitem__('past_key_values', past_kvs)  # KV缓存

        return self.OUT

    @torch.inference_mode()
    def generate(self, input_ids, eos_token_id=2, max_new_tokens=1024, temperature=0.75, top_p=0.90,
                 stream=False, rp=1., use_cache=True, pad_token_id=0, num_return_sequences=1, **args):
        """
        文本生成函数

        参数:
            input_ids: 输入的token ID，形状为[batch_size, seq_len]
            eos_token_id: 结束符token的ID，默认为2
            max_new_tokens: 最大生成的新token数量，默认为1024
            temperature: 温度参数，控制生成的随机性，越高越随机，默认为0.75
            top_p: 核采样参数，控制采样的token范围，默认为0.90
            stream: 是否使用流式生成，默认为False
            rp: 重复惩罚因子，降低已出现token的概率，默认为1.0
            use_cache: 是否使用KV缓存加速生成，默认为True
            pad_token_id: 填充token的ID，默认为0
            num_return_sequences: 每个输入生成的序列数量，默认为1
            **args: 其他参数

        返回:
            生成的token序列，形状为[batch_size*num_return_sequences, seq_len]
        """
        # 流式生成模式：逐token生成并返回生成器
        if stream:
            return self._stream(input_ids, eos_token_id, max_new_tokens, temperature, top_p, rp, use_cache, **args)

        # 直接生成模式：一次性返回所有生成结果
        generated = []
        for i in range(input_ids.size(0)):
            # 移除每个输入序列中的填充token
            non_pad = input_ids[i][input_ids[i] != pad_token_id].unsqueeze(0)
            # 为每个输入生成num_return_sequences个不同序列
            for _ in range(num_return_sequences):
                # 使用流式生成函数获取生成结果
                out = self._stream(non_pad, eos_token_id, max_new_tokens, temperature, top_p, rp, use_cache, **args)
                # 收集生成的token
                tokens_list = [tokens[:, -1:] for tokens in out]
                # 将生成的token拼接起来
                gen = torch.cat(tokens_list, dim=-1) if tokens_list else non_pad
                # 将输入和生成的token拼接成完整序列
                full_sequence = torch.cat([non_pad, gen], dim=-1)
                generated.append(full_sequence)

        # 找出所有生成序列中的最大长度
        max_length = max(seq.size(1) for seq in generated)
        # 将所有序列填充到相同长度
        generated = [
            torch.cat(
                [seq, torch.full((1, max_length - seq.size(1)), pad_token_id, dtype=seq.dtype, device=seq.device)],
                dim=-1)
            for seq in generated
        ]
        # 将所有序列拼接成一个批次
        output = torch.cat(generated, dim=0)
        # 重塑为[batch_size*num_return_sequences, seq_len]形状
        res = output.view(input_ids.size(0) * num_return_sequences, -1)
        return res

    def _stream(self, input_ids, eos_token_id, max_new_tokens, temperature, top_p, rp, use_cache, **args):
        """
        流式生成函数，逐token生成并返回生成器

        参数:
            input_ids: 输入的token ID
            eos_token_id: 结束符token的ID
            max_new_tokens: 最大生成的新token数量
            temperature: 温度参数，控制随机性
            top_p: 核采样参数
            rp: 重复惩罚因子
            use_cache: 是否使用KV缓存
            **args: 其他参数

        返回:
            生成器，每次产出当前已生成的token序列
        """
        # 记录起始位置、是否首次推理和KV缓存
        start, first_seq, past_kvs = input_ids.shape[1], True, None
        # 循环生成，直到达到最大长度或生成结束符
        while input_ids.shape[1] < max_new_tokens - 1:
            # 首次推理或不使用缓存时，处理整个序列
            if first_seq or not use_cache:
                out, first_seq = self(input_ids, past_key_values=past_kvs, use_cache=use_cache, **args), False
            # 后续推理且使用缓存时，只处理最新的token
            else:
                out = self(input_ids[:, -1:], past_key_values=past_kvs, use_cache=use_cache,
                           start_pos=input_ids.shape[1] - 1, **args)
            # 获取logits和更新后的KV缓存
            logits, past_kvs = out.logits[:, -1, :], out.past_key_values

            # 重复惩罚：降低已出现token的概率
            logits[:, list(set(input_ids.tolist()[0]))] /= rp
            # 应用温度缩放
            logits /= (temperature + 1e-9)

            # 核采样(Top-p/nucleus sampling)：只保留概率最高的若干token，使其累积概率达到top_p
            if top_p is not None and top_p < 1.0:
                # 对logits按降序排序
                sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)
                # 计算softmax概率
                sorted_probs = F.softmax(sorted_logits, dim=-1)
                # 计算累积概率
                cumulative_probs = torch.cumsum(sorted_probs, dim=-1)
                # 找出累积概率超过top_p的位置
                sorted_indices_to_remove = cumulative_probs > top_p
                # 向右移动一位，保留第一个超过阈值的token
                sorted_indices_to_remove[:, 1:] = sorted_indices_to_remove[:, :-1].clone()
                # 确保至少保留概率最高的token
                sorted_indices_to_remove[:, 0] = False
                # 将排序后的掩码映射回原始顺序
                indices_to_remove = sorted_indices_to_remove.scatter(1, sorted_indices, sorted_indices_to_remove)
                # 将被过滤的token概率设为负无穷
                logits[indices_to_remove] = -float('Inf')

            # 根据处理后的logits进行多项式采样，选择下一个token
            input_ids_next = torch.multinomial(F.softmax(logits, dim=-1), num_samples=1)
            # 将新token添加到序列中
            input_ids = torch.cat((input_ids, input_ids_next), dim=1)
            # 产出当前已生成的序列
            yield input_ids[:, start:]
            # 如果生成了结束符，则停止生成
            if input_ids_next.item() == eos_token_id:
                break
