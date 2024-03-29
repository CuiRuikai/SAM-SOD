import math
import torch
import torch.nn as nn
from torch.nn.parameter import Parameter
from segment_anything.modeling import Sam
from safetensors import safe_open
from methods.segment_anything import sam_model_registry
import loralib

custom_config = {'base'      : {'strategy': 'base_adam',
                                'batch': 2,
                               },
                 'customized': {'--ckpt_path': {'type': str, 'default': './ckpts/sam_vit_b_01ec64.pth'},
                                '--model_type': {'type': str, 'default': 'vit_b'},
                                '--rank': {'type': int, 'default': 12},
                                '--target_rank': {'type': int, 'default': 8},
                               },
                }


class _AdaLoRA_qkv(nn.Module):
    def __init__(self, qkv, linear_q, linear_v):
        super().__init__()
        self.qkv = qkv
        self.linear_q = linear_q
        self.linear_v = linear_v
        self.dim = qkv.in_features

    def forward(self, x):
        qkv = self.qkv(x)  # B,N,N,3*org_C
        new_q = self.linear_q(x)
        new_v = self.linear_v(x)
        qkv[:, :, :, : self.dim] += new_q
        qkv[:, :, :, -self.dim:] += new_v
        return qkv


class Network(nn.Module):
    def __init__(self, config, encoder, feat):
        super(Network, self).__init__()

        sam_checkpoint = config['ckpt_path']
        model_type = config['model_type']
        sam_model = sam_model_registry[model_type](checkpoint=sam_checkpoint)

        r = config['rank']

        assert r > 0
        # base_vit_dim = sam_model.image_encoder.patch_embed.proj.out_channels
        # dim = base_vit_dim
        self.lora_layer = list(range(len(sam_model.image_encoder.blocks)))  # Only apply lora to the image encoder by default
        # create for storage, then we can init them or load weights

        # lets freeze first
        for param in sam_model.image_encoder.parameters():
            param.requires_grad = False

        # Here, we do the surgery
        for t_layer_i, blk in enumerate(sam_model.image_encoder.blocks):
            # If we only want few lora layer instead of all
            if t_layer_i not in self.lora_layer:
                continue
            w_qkv_linear = blk.attn.qkv
            self.dim = w_qkv_linear.in_features
            linear_q = loralib.SVDLinear(in_features=self.dim, out_features=self.dim, r=r, bias=False)
            linear_v = loralib.SVDLinear(in_features=self.dim, out_features=self.dim, r=r, bias=False)
            blk.attn.qkv = _AdaLoRA_qkv(
                w_qkv_linear,
                linear_q,
                linear_v
            )
        self.sam = sam_model
        # loralib.mark_only_lora_as_trainable(self.sam)


    def forward(self, x, phase='test'):
        batched_input = x
        image_size = batched_input.shape[-1]
        out = self.sam(batched_input, multimask_output=False, image_size=image_size)
        out_dict = {'sal': out['masks'], 'final': out['masks']}
        return out_dict