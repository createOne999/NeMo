# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import copy
import math
import types
from contextlib import nullcontext
from typing import List, Optional, Tuple, Union

import torch
import torch.nn.functional as F
from PIL import Image as PIL_Image
from megatron.core import InferenceParams, parallel_state
from megatron.core import tensor_parallel
from megatron.core.fusions.fused_bias_dropout import get_bias_dropout_add
from megatron.core.packed_seq_params import PackedSeqParams
from megatron.core.transformer.attention import SelfAttention, SelfAttentionSubmodules
from megatron.core.transformer.custom_layers.transformer_engine import (
    TEColumnParallelLinear,
    TEDotProductAttention,
    TERowParallelLinear,
)
from megatron.core.transformer.enums import AttnMaskType
from megatron.core.transformer.identity_op import IdentityOp
from megatron.core.transformer.mlp import MLP, MLPSubmodules
from megatron.core.transformer.module import MegatronModule
from megatron.core.transformer.spec_utils import ModuleSpec
from megatron.core.transformer.spec_utils import build_module
from megatron.core.transformer.transformer_block import TransformerBlock
from megatron.core.transformer.transformer_config import TransformerConfig
from megatron.core.transformer.transformer_layer import TransformerLayer
from megatron.core.transformer.transformer_layer import TransformerLayerSubmodules
from megatron.core.utils import (
    make_viewless_tensor,
)
from torch import Tensor
from torch import nn

try:
    from megatron.core.transformer.custom_layers.transformer_engine import (
        TEDelayedScaling,
        TENorm,
        get_cpu_offload_context,
        te_checkpoint,
    )

    HAVE_TE = True
    LayerNormImpl = TENorm
except ImportError:
    HAVE_TE = False
    get_cpu_offload_context = None
    try:
        import apex

        LayerNormImpl = FusedLayerNorm
    except ModuleNotFoundError:
        from megatron.core.transformer.torch_layer_norm import WrappedTorchLayerNorm

        LayerNormImpl = WrappedTorchLayerNorm


def _get_full_row_masked_out_mask(
        attn_bias,
        negative_inf_value,
):
    """
    attn_bias should be a 4D tensor of shape [B, H, S1, S2]
    where B is the batch size, H is the number of heads,
    and S1/S2 are the sequence lengths. This returns
    a 4D tensor of shape [B, H, S1, 1] which stores boolean
    values which are 0 if the a full row in the last dimension
    contains negative infinity values, otherwise it's 1.
    """
    return (attn_bias != negative_inf_value).any(dim=-1).type_as(attn_bias)[..., None]


def get_negative_inf_value(dtype):
    return torch.finfo(dtype).min


def to_2tuple(x):
    if isinstance(x, collections.abc.Iterable):
        return x
    return (x, x)


def _stack_images(
        images: List[List[PIL_Image.Image]],
        max_num_chunks: int,
        image_res: int,
        max_num_images: int,
) -> Tuple[torch.Tensor, List[int]]:
    """
    Takes a list of list of images and stacks them into a tensor.
    This function is needed since images can be of completely
    different resolutions and aspect ratios.
    """
    out_images, out_num_chunks = [], []
    for imgs_sample in images:
        out_images_i = torch.zeros(
            max_num_images,
            max_num_chunks,
            3,
            image_res,
            image_res,
        )
        _num_chunks = []
        for j, chunks_image in enumerate(imgs_sample):
            out_images_i[j, : chunks_image.shape[0]] = chunks_image
            _num_chunks.append(chunks_image.shape[0])
        out_images.append(out_images_i)
        out_num_chunks.append(_num_chunks)
    return torch.stack(out_images), out_num_chunks


def _pad_masks(
        all_masks: List[List[List[int]]],
        all_num_chunks: List[List[int]],
        total_len: int,
        max_num_chunks: int,
        dtype=torch.bfloat16,
) -> torch.Tensor:
    inf_value = get_negative_inf_value(dtype)

    bsz = len(all_masks)
    max_num_media = max([len(m) for m in all_masks])

    out_masks = torch.full(
        (bsz, total_len, max_num_media, max_num_chunks),
        inf_value,
        dtype=dtype,
    )

    for idx, (mask, num_chunks) in enumerate(zip(all_masks, all_num_chunks)):
        for mask_idx, (mask_elem, mask_num_chunks) in enumerate(zip(mask, num_chunks)):
            if len(mask_elem) == 2:
                mask_elem[1] = min(mask_elem[1], total_len)
                if mask_elem[1] == -1:
                    mask_elem[1] = total_len
                out_masks[
                idx, mask_elem[0]: mask_elem[1], mask_idx, :mask_num_chunks
                ].fill_(0.0)

    return out_masks


def create_vision_mask_tensor(tokens: torch.Tensor, vision_token_id: int) -> torch.Tensor:
    """
    Create a vision mask from a tensor of tokens and a vision token ID.

    Args:
        tokens (torch.Tensor): A 1D tensor of token IDs.
        vision_token_id (int): The ID of the vision token.

    Returns:
        torch.Tensor: A tensor containing vision masks in the format [start, end].
    """
    # Get the locations of the vision tokens
    vision_token_locations = (tokens == vision_token_id).nonzero(as_tuple=False).squeeze()

    # If no vision token found, return an empty tensor
    if vision_token_locations.numel() == 0:
        return torch.empty(0, 2, dtype=torch.long)

    vision_masks = []

    # Handle case with only one vision token
    if vision_token_locations.numel() == 1:
        vision_masks.append([vision_token_locations.item(), len(tokens)])
    else:
        # Multiple vision tokens, pairwise masks
        for i in range(len(vision_token_locations) - 1):
            vision_masks.append([vision_token_locations[i].item(), vision_token_locations[i + 1].item()])
        # Last vision token attends to all subsequent text
        vision_masks.append([vision_token_locations[-1].item(), len(tokens)])

    # Handle consecutive vision tokens
    last_mask_end = vision_masks[-1][1]
    for vision_mask in reversed(vision_masks):
        if vision_mask[0] == vision_mask[1] - 1:
            vision_mask[1] = last_mask_end
        last_mask_end = vision_mask[1]

    return torch.tensor(vision_masks, dtype=torch.long)


def apply_scaling(freqs: torch.Tensor):
    # Values obtained from grid search
    scale_factor = 8
    low_freq_factor = 1
    high_freq_factor = 4
    old_context_len = 8192  # original llama3 length

    low_freq_wavelen = old_context_len / low_freq_factor
    high_freq_wavelen = old_context_len / high_freq_factor
    new_freqs = []
    for freq in freqs:
        wavelen = 2 * math.pi / freq
        if wavelen < high_freq_wavelen:
            new_freqs.append(freq)
        elif wavelen > low_freq_wavelen:
            new_freqs.append(freq / scale_factor)
        else:
            assert low_freq_wavelen != high_freq_wavelen
            smooth = (old_context_len / wavelen - low_freq_factor) / (
                    high_freq_factor - low_freq_factor
            )
            new_freqs.append((1 - smooth) * freq / scale_factor + smooth * freq)
    return torch.tensor(new_freqs, dtype=freqs.dtype, device=freqs.device)


# Use this spec for an implementation using modules in TE
def get_image_transformer_layer_spec() -> ModuleSpec:
    image_transformer_submodules = TransformerLayerSubmodules(
        input_layernorm=TENorm,
        self_attention=ModuleSpec(
            module=SelfAttentionNoBias,
            params={"attn_mask_type": AttnMaskType.no_mask},
            submodules=SelfAttentionSubmodules(
                linear_qkv=TEColumnParallelLinear,
                core_attention=TEDotProductAttention,
                linear_proj=TERowParallelLinear,
                q_layernorm=IdentityOp,
                k_layernorm=IdentityOp,
            ),
        ),
        self_attn_bda=get_bias_dropout_add,
        pre_mlp_layernorm=TENorm,
        mlp=ModuleSpec(
            module=MLP, submodules=MLPSubmodules(linear_fc1=TEColumnParallelLinear, linear_fc2=TERowParallelLinear, ),
        ),
        mlp_bda=get_bias_dropout_add,
    )
    return ModuleSpec(module=ImageTransformerLayer, submodules=image_transformer_submodules)


def forward_with_return_intermediate(
        self,
        hidden_states: Tensor,
        attention_mask: Tensor,
        context: Tensor = None,
        context_mask: Tensor = None,
        rotary_pos_emb: Tensor = None,
        inference_params: InferenceParams = None,
        packed_seq_params: PackedSeqParams = None,
        return_intermediate: List[int] = None
):
    # hidden_states (float): [s, b, h]
    # attention_mask (bool): [1, 1, s, s]

    if not self.pre_process:
        # See set_input_tensor()
        hidden_states = self.input_tensor

    hidden_states = make_viewless_tensor(inp=hidden_states, requires_grad=True, keep_graph=True)

    if self.config.sequence_parallel:
        rng_context = tensor_parallel.get_cuda_rng_tracker().fork()
    else:
        rng_context = nullcontext()

    if self.config.fp8:
        import transformer_engine  # To keep out TE dependency when not training in fp8

        if self.config.fp8 == "e4m3":
            fp8_format = transformer_engine.common.recipe.Format.E4M3
        elif self.config.fp8 == "hybrid":
            fp8_format = transformer_engine.common.recipe.Format.HYBRID
        else:
            raise ValueError("E4M3 and HYBRID are the only supported FP8 formats.")

        fp8_recipe = TEDelayedScaling(
            config=self.config,
            fp8_format=fp8_format,
            override_linear_precision=(False, False, not self.config.fp8_wgrad),
        )
        fp8_group = None
        if parallel_state.model_parallel_is_initialized():
            fp8_group = parallel_state.get_amax_reduction_group(with_context_parallel=True)
        fp8_context = transformer_engine.pytorch.fp8_autocast(
            enabled=True, fp8_recipe=fp8_recipe, fp8_group=fp8_group
        )
    else:
        fp8_context = nullcontext()

    with rng_context and fp8_context:
        # Forward pass.
        if self.config.recompute_granularity == 'full' and self.training:
            assert return_intermediate is None, "Config `return_intermediate` cannot be used with " \
                                                "`recompute_granularity='full'`. "
            hidden_states = self._checkpointed_forward(
                hidden_states=hidden_states,
                attention_mask=attention_mask,
                context=context,
                context_mask=context_mask,
                rotary_pos_emb=rotary_pos_emb,
                packed_seq_params=packed_seq_params,
            )
        else:
            intermediate_hidden_states = []
            for l_no, layer in enumerate(self.layers):
                if return_intermediate is not None and l_no in return_intermediate:
                    intermediate_hidden_states.append(hidden_states)

                with self.offload_context:
                    if (len(self.cuda_graphs) == 0) or (not self.training):
                        hidden_states, context = layer(
                            hidden_states=hidden_states,
                            attention_mask=attention_mask,
                            context=context,
                            context_mask=context_mask,
                            rotary_pos_emb=rotary_pos_emb,
                            inference_params=inference_params,
                            packed_seq_params=packed_seq_params,
                        )
                        # CUDA graph doesn't output context and is expected to be None
                        assert (
                                (context is None)
                                or (not self.config.enable_cuda_graph)
                                or (not self.training)
                        )
                    else:
                        # CUDA graph replay for layer `l_no` and microbatch `self.current_microbatch`
                        # CUDA graph requires positional arguments with the exception of is_first_microbatch.
                        # Also CUDA graph accepts only Tensor inputs and outputs. Hence, the arg list and
                        # returned list is limited to `hidden_states`.
                        assert (len(self.cuda_graphs) > l_no) and (
                                self.current_microbatch < len(self.cuda_graphs[l_no])
                        )
                        hidden_states = self.cuda_graphs[l_no][self.current_microbatch](
                            hidden_states, is_first_microbatch=(self.current_microbatch == 0)
                        )

                if (
                        torch.is_grad_enabled()
                        and self.config.cpu_offloading
                        and self.group_prefetch_offload_commit_async is not None
                ):
                    hidden_states = self.group_prefetch_offload_commit_async(hidden_states)

        # Final layer norm.
        if self.final_layernorm is not None:
            hidden_states = self.final_layernorm(hidden_states)
            # TENorm produces a "viewed" tensor. This will result in schedule.py's
            # deallocate_output_tensor() throwing an error, so a viewless tensor is
            # created to prevent this.
            hidden_states = make_viewless_tensor(
                inp=hidden_states, requires_grad=True, keep_graph=True
            )

        if return_intermediate is not None:
            return hidden_states, torch.stack(intermediate_hidden_states, dim=-1)

        return hidden_states


class ColumnParallelConv2dPatch(MegatronModule):
    """Conv2D Patching layer with model parallelism.
    Column parallel over unfolded input.
    Arguments:
        in_channels: Input channels.
        out_channels: Output channels.
        kernel_size: Size of convolution kernel.
        stride (default 1): Stride for convolution.
        bias (default False): Use bias in Conv2d.
    Input: (bsz, in_channels, width, height)
    Output: (bsz, num_tokens, out_channels)
    """

    def __init__(
            self,
            config: TransformerConfig,
            in_channels: int,
            out_channels: int,
            kernel_size: Union[int, Tuple[int, int]],
            stride: Union[int, Tuple[int, int]],
            bias: Optional[bool] = False,
    ) -> None:
        super().__init__(config=config)
        if isinstance(kernel_size, int):
            kernel_size = (kernel_size, kernel_size)
        self._unfold = torch.nn.Unfold(kernel_size=kernel_size, stride=stride)
        self._linear = TEColumnParallelLinear(
            in_channels * kernel_size[0] * kernel_size[1],
            out_channels,
            bias=bias,
            config=self.config,
            init_method=self.config.init_method,
            gather_output=False,
            skip_bias_add=False,
            is_expert=False,
            tp_comm_buffer_name='conv1',
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self._unfold(x)
        x = x.permute(0, 2, 1)
        x = F.linear(x, self._linear.weight)
        x = tensor_parallel.gather_from_tensor_model_parallel_region(x)
        return x


class PrecomputedTilePositionEmbedding(torch.nn.Module):
    def __init__(
            self,
            config: TransformerConfig,
            gated: bool = False,
    ):
        super().__init__()
        self.max_num_tiles = config.max_num_tiles
        self.hidden_size = config.hidden_size
        self.max_aspect_ratio_id = config.max_aspect_ratio_id

        self.embedding = nn.Embedding(self.max_aspect_ratio_id + 1, self.max_num_tiles * self.hidden_size)
        self.gated = gated
        if gated:
            self.gate = nn.Parameter(torch.zeros(1))

    def forward(self, hidden_states: torch.Tensor, aspect_ratio_ids: torch.Tensor) -> torch.Tensor:
        embeddings = self.embedding(aspect_ratio_ids)
        embeddings = embeddings.reshape(-1, self.max_num_tiles, 1, self.hidden_size)

        if self.gated:
            embeddings = embeddings * self.gate.tanh()

        hidden_states = hidden_states + embeddings
        return hidden_states


class SelfAttentionNoBias(SelfAttention):
    """Self-attention layer class without bias"""

    def __init__(
            self,
            config: TransformerConfig,
            submodules: SelfAttentionSubmodules,
            layer_number: int,
            attn_mask_type=AttnMaskType.padding,
    ):
        super().__init__(
            config=config,
            submodules=submodules,
            layer_number=layer_number,
            attn_mask_type=attn_mask_type,
        )

        # Override to remove bias since we don't have a good config for this.
        self.linear_qkv = build_module(
            submodules.linear_qkv,
            self.config.hidden_size,
            self.query_projection_size + 2 * self.kv_projection_size,
            config=self.config,
            init_method=self.config.init_method,
            gather_output=False,
            bias=False,
            skip_bias_add=False,
            is_expert=False,
            tp_comm_buffer_name='qkv',
        )

        self.linear_proj = build_module(
            submodules.linear_proj,
            self.query_projection_size,
            self.config.hidden_size,
            config=self.config,
            init_method=self.config.output_layer_init_method,
            bias=False,
            input_is_parallel=True,
            skip_bias_add=True,
            is_expert=False,
            tp_comm_buffer_name='proj',
        )


class ImageTransformerLayer(TransformerLayer):
    def __init__(
            self,
            config: TransformerConfig,
            submodules: TransformerLayerSubmodules,
            layer_number: int = 1,
            hidden_dropout: float = None,
    ):
        super().__init__(
            config=config,
            submodules=submodules,
            layer_number=layer_number,
            hidden_dropout=hidden_dropout,
        )
        self.gated = self.config.gated
        if self.gated:
            self.gate_attn = nn.Parameter(torch.zeros(1, dtype=self.config.params_dtype))
            self.gate_ffn = nn.Parameter(torch.zeros(1, dtype=self.config.params_dtype))

    def forward(
            self,
            hidden_states,
            attention_mask,
            context=None,
            context_mask=None,
            rotary_pos_emb=None,
            inference_params=None,
            packed_seq_params=None,
    ):
        # hidden_states: [s, b, h]

        # Residual connection.
        residual = hidden_states

        # Optional Input Layer norm
        input_layernorm_output = self.input_layernorm(hidden_states)

        # Self attention.
        attention_output_with_bias = self.self_attention(
            input_layernorm_output,
            attention_mask=attention_mask,
            inference_params=inference_params,
            rotary_pos_emb=rotary_pos_emb,
            packed_seq_params=packed_seq_params,
        )

        _gate_attn = 1 if not self.gated else self.gate_attn.tanh()
        assert isinstance(attention_output_with_bias,
                          tuple), "`attention_output_with_bias` needs to be tuple for gating."
        attention_output_with_bias = tuple(
            _gate_attn * output if output is not None else None
            for output in attention_output_with_bias
        )

        with self.bias_dropout_add_exec_handler():
            hidden_states = self.self_attn_bda(self.training, self.config.bias_dropout_fusion)(
                attention_output_with_bias, residual, self.hidden_dropout
            )

        # Residual connection.
        residual = hidden_states

        # Optional Layer norm post the cross-attention.
        pre_mlp_layernorm_output = self.pre_mlp_layernorm(hidden_states)

        # MLP.
        mlp_output_with_bias = self.mlp(pre_mlp_layernorm_output)

        _gate_ffn = 1 if not self.gated else self.gate_ffn.tanh()
        assert isinstance(mlp_output_with_bias,
                          tuple), "`mlp_output_with_bias` needs to be tuple for gating."
        mlp_output_with_bias = tuple(
            _gate_ffn * output if output is not None else None
            for output in mlp_output_with_bias
        )

        with self.bias_dropout_add_exec_handler():
            hidden_states = self.mlp_bda(self.training, self.config.bias_dropout_fusion)(
                mlp_output_with_bias, residual, self.hidden_dropout
            )

        output = make_viewless_tensor(
            inp=hidden_states, requires_grad=hidden_states.requires_grad, keep_graph=True
        )

        return output, context


class VisionEncoder(MegatronModule):
    def __init__(
            self,
            config: TransformerConfig,
            image_size: int = 560,
            patch_size: int = 14,
            in_channels: int = 3,
            pre_process: bool = True,
            post_process: bool = True,
            return_intermediate=None,
    ):
        super().__init__(config=config)
        self.return_intermediate = return_intermediate
        self.image_size = to_2tuple(image_size)
        self.patch_size = to_2tuple(patch_size)
        self.grid_size = (
            self.image_size[0] // self.patch_size[0],
            self.image_size[1] // self.patch_size[1],
        )
        self.pre_process = pre_process
        self.post_process = post_process

        self.max_aspect_ratio_id = self.config.max_aspect_ratio_id
        self.max_num_tiles = config.max_num_tiles
        width = config.hidden_size
        self.conv1 = ColumnParallelConv2dPatch(
            config=config,
            in_channels=in_channels,
            out_channels=width,
            kernel_size=patch_size,
            stride=patch_size,
            bias=False,
        )
        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(
            scale * torch.randn(self.grid_size[0] * self.grid_size[1] + 1, width)
        )
        self.ln_post = LayerNormImpl(config=config, hidden_size=width)
        self.ln_pre = LayerNormImpl(config=config, hidden_size=width)
        self.transformer = TransformerBlock(
            config=self.config,
            spec=get_image_transformer_layer_spec(),
            post_layer_norm=False,
            pre_process=self.pre_process,
            post_process=self.post_process,
        )
        self.transformer.forward = types.MethodType(forward_with_return_intermediate, self.transformer)
        # pre and post tile position embedding
        global_config = copy.deepcopy(self.config)
        global_config.num_layers = self.config.num_global_layers
        global_config.gated = True
        self.global_transformer = TransformerBlock(
            config=global_config,
            spec=get_image_transformer_layer_spec(),
            post_layer_norm=False,
            pre_process=self.pre_process,
            post_process=self.post_process,
        )
        # pre and post tile position embedding
        self.pre_tile_pos_embed = PrecomputedTilePositionEmbedding(
            config=config,
            gated=True,
        )
        self.post_tile_pos_embed = PrecomputedTilePositionEmbedding(
            config=config,
            gated=True,
        )
        self.gated_tile_positional_embedding = nn.Embedding(
            self.max_aspect_ratio_id + 1,
            self.max_num_tiles * (self.grid_size[0] * self.grid_size[1] + 1) * width
        )
        self.gated_positional_embedding_gate = nn.Parameter(torch.zeros(1))

    def apply_positional_embedding(self, x, aspect_ratio_ids):
        # apply regular position embedding
        bsz, num_chunks, num_tokens, dim = x.shape
        x = x.view(bsz * num_chunks, num_tokens, dim)
        x = x + self.positional_embedding * (
                1 - self.gated_positional_embedding_gate.tanh()
        )
        x = x.view(bsz, num_chunks, num_tokens, dim)
        tile_position_embedding = self.gated_tile_positional_embedding(aspect_ratio_ids)
        tile_position_embedding = tile_position_embedding.reshape(
            bsz, num_chunks, num_tokens, dim
        )
        x = x + self.gated_positional_embedding_gate.tanh() * tile_position_embedding
        return x

    def apply_class_embedding(self, x):
        x = torch.cat(
            [
                self.class_embedding.to(x.dtype)
                + torch.zeros(
                    x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device
                ),
                x,
            ],
            dim=1,
        )  # shape = [*, grid ** 2 + 1, width]
        return x

    def forward(self, images: torch.Tensor, ar_ids: torch.Tensor) -> torch.Tensor:
        if images.ndim == 5:
            num_concurrent_media = 1
            bsz, num_chunks, nch, w, h = images.shape
        else:
            bsz, num_concurrent_media, num_chunks, nch, w, h = images.shape

        images = images.reshape(bsz * num_concurrent_media * num_chunks, nch, w, h)
        ar_ids = ar_ids.reshape(bsz * num_concurrent_media, 1)

        # patch embedding
        x = images.reshape(bsz * num_concurrent_media * num_chunks, nch, w, h)
        x = self.conv1(x)  # shape = [*, width, grid ** 2]
        _, ntok, dim = x.shape
        x = x.reshape(bsz * num_concurrent_media, num_chunks, ntok, dim)

        # tile embeddings
        x = self.pre_tile_pos_embed(x, ar_ids)
        x = x.reshape(bsz * num_concurrent_media * num_chunks, ntok, dim)

        # apply cls token
        x = self.apply_class_embedding(x)
        ntok += 1

        # apply position embeddings
        x = x.reshape(bsz * num_concurrent_media, num_chunks, ntok, dim)
        x = self.apply_positional_embedding(x, ar_ids)

        x = self.ln_pre(x)
        npad, attn_mask = 0, None
        # TODO(yuya): padding for parallism and perf
        # x, npad = expand_num_tokens_to_mult8(x)
        # attn_mask = build_encoder_attention_mask(x, ar, ntok, num_chunks, 1)
        x = x.view(bsz * num_concurrent_media, -1, dim)
        # TODO(yuya): optimize the transposes
        x = x.transpose(0, 1).contiguous()
        x, int_x = self.transformer(
            hidden_states=x,
            attention_mask=attn_mask,
            return_intermediate=self.return_intermediate,
        )
        x, int_x = x.transpose(0, 1).contiguous(), int_x.transpose(1, 2)
        x = self.ln_post(x)
        x = x.reshape(bsz * num_concurrent_media, num_chunks, ntok + npad, dim)
        x = self.post_tile_pos_embed(x, ar_ids)
        x = x.reshape(bsz * num_concurrent_media, num_chunks * (ntok + npad), dim)
        x = x.transpose(0, 1).contiguous()
        x = self.global_transformer(
            hidden_states=x,
            attention_mask=attn_mask
        )
        x = x.transpose(0, 1)
        x = x.reshape(bsz * num_concurrent_media, num_chunks, ntok + npad, dim)
        # x = contract_num_tokens_from_mult8(x, npad)

        # adding back intermediate layer outputs
        x = x.reshape(bsz, num_concurrent_media, num_chunks, ntok, dim)
        int_x = int_x.reshape(bsz * num_concurrent_media, num_chunks, ntok + npad, -1)
        # int_x = contract_num_tokens_from_mult8(int_x, npad)
        int_x = int_x.reshape(bsz, num_concurrent_media, num_chunks, ntok, -1)
        x = torch.cat([x, int_x], dim=-1)
        return x