# -----------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Zhipeng Zhang (zhangzhipeng2017@ia.ac.cn)
# Revised by Jilai Zheng, for USOT
# ------------------------------------------------------------------------------
import torch.nn as nn
import torch.nn.functional as F
import torch
from lib.models.prroi_pool import PrRoIPool2D
import math
from timm.models.layers import to_2tuple, trunc_normal_

class matrix(nn.Module):
    """
    Encode feature for multi-scale correlation
    """
    def __init__(self, in_channels, out_channels):
        super(matrix, self).__init__()

        # Same size: h, w
        self.matrix11_k = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )
        self.matrix11_s = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

        # Dilated size: h/2, w
        self.matrix12_k = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, kernel_size=3, bias=False, dilation=(2, 1)),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )
        self.matrix12_s = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, kernel_size=3, bias=False, dilation=(2, 1)),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

        # Dilated size: w/2, h
        self.matrix21_k = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, kernel_size=3, bias=False, dilation=(1, 2)),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )
        self.matrix21_s = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, kernel_size=3, bias=False, dilation=(1, 2)),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, z=None, x=None):

        if x is not None:
            x11 = self.matrix11_s(x)
            x12 = self.matrix12_s(x)
            x21 = self.matrix21_s(x)

        if z is not None:
            z11 = self.matrix11_k(z)
            z12 = self.matrix12_k(z)
            z21 = self.matrix21_k(z)

        if x is not None and z is not None:
            return [z11, z12, z21], [x11, x12, x21]
        elif z is not None:
            return [z11, z12, z21], None
        elif x is not None:
            return None, [x11, x12, x21]
        else:
            return None, None


class GroupDW(nn.Module):
    """
    Encode backbone feature
    """

    def __init__(self, in_channels=256):
        super(GroupDW, self).__init__()
        self.weight = nn.Parameter(torch.ones(3))

    def forward(self, z, x):
        z11, z12, z21 = z
        x11, x12, x21 = x

        re11 = xcorr_depthwise(x11, z11)
        re12 = xcorr_depthwise(x12, z12)
        re21 = xcorr_depthwise(x21, z21)
        re = [re11, re12, re21]

        # Weight
        weight = F.softmax(self.weight, 0)

        s = 0
        for i in range(3):
            s += weight[i] * re[i]

        return s

class Conf_Fusion(nn.Module):
    """
    Fusion N_mem memory features with confidence-value paradigm
    """

    def __init__(self, in_channels=256, out_channels=256):
        super(Conf_Fusion, self).__init__()

        self.conf_gen = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )
        self.value_gen = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x):
        batch, mem_size, channel, h, w = x.shape
        x = x.view(-1, channel, h, w)

        # Calc confidence on each position
        # confidence = self.conf_gen(x)
        # confidence = torch.clamp(confidence, max=4, min=-6)
        # # Softmax each confidence map across all confidence maps
        # confidence = torch.exp(confidence)
        # confidence = confidence.view(batch, mem_size, channel, h, w)
        # confidence_sum = confidence.sum(dim=1).view(batch, 1, channel, h, w).repeat(1, mem_size, 1, 1, 1)
        # confidence_norm = confidence / confidence_sum

        # The raw value for output (not weighted yet)
        value = self.value_gen(x)
        value = value.view(batch, mem_size, channel, h, w)

        # Weighted sum of the value maps, with confidence maps as element-wise weights
        # out = confidence_norm * value
        out = value
        out = out.sum(dim=1)

        return out


def xcorr_depthwise(x, kernel):
    """
    Depth-wise cross correlation
    """
    batch, channel, h_k, w_k = kernel.shape
    _, _, h_x, w_x = x.shape
    x = x.view(-1, batch*channel, h_x, w_x)
    kernel = kernel.view(batch*channel, 1, h_k, w_k)
    out = F.conv2d(x, kernel, groups=batch*channel)
    out = out.view(-1, channel, out.size(2), out.size(3))
    return out

def conv3x3(in_planes, out_planes, stride=1, bias=True):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=bias)

def conv3x3_bn_relu(in_planes, out_planes, stride=1, bias=True):
    """3x3 convolution with padding, batch normalization and relu"""
    block = nn.Sequential(
        conv3x3(in_planes, out_planes, stride, bias),
        nn.BatchNorm2d(out_planes),
        nn.ReLU(inplace=True)
    )
    return block


class box_tower_reg(nn.Module):
    """
    Box tower for FCOS reg
    """
    def __init__(self, in_channels=512, out_channels=256, tower_num=1):
        super(box_tower_reg, self).__init__()
        tower = []
        cls_tower = []
        cls_memory_tower = []
        # Layers for multi-scale correlation
        self.cls_encode = matrix(in_channels=in_channels, out_channels=out_channels)
        self.reg_encode = matrix(in_channels=in_channels, out_channels=out_channels)
        self.cls_dw = GroupDW(in_channels=in_channels)
        self.reg_dw = GroupDW(in_channels=in_channels)
        # Layers for confidence-value integration
        self.conf_fusion = Conf_Fusion(in_channels=out_channels, out_channels=out_channels)

        # Box pred tower
        for i in range(tower_num):
            if i == 0:
                tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))
            else:
                tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))

            tower.append(nn.BatchNorm2d(out_channels))
            tower.append(nn.ReLU())

        # Cls tower
        for i in range(tower_num):
            if i == 0:
                cls_tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))
            else:
                cls_tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))

            cls_tower.append(nn.BatchNorm2d(out_channels))
            cls_tower.append(nn.ReLU())

        # Memory cls tower
        for i in range(tower_num):
            if i == 0:
                cls_memory_tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))
            else:
                cls_memory_tower.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1))

            cls_memory_tower.append(nn.BatchNorm2d(out_channels))
            cls_memory_tower.append(nn.ReLU())

        self.add_module('bbox_tower', nn.Sequential(*tower))
        self.add_module('cls_tower', nn.Sequential(*cls_tower))
        self.add_module('cls_memory_tower', nn.Sequential(*cls_memory_tower))

        # Reg head
        self.bbox_pred = nn.Conv2d(out_channels, 4, kernel_size=3, stride=1, padding=1)
        # Cls head
        self.cls_pred = nn.Conv2d(out_channels, 1, kernel_size=3, stride=1, padding=1)
        self.cls_memory_pred = nn.Conv2d(out_channels, 1, kernel_size=3, stride=1, padding=1)

        # Adjust scale for reg
        self.adjust = nn.Parameter(0.1 * torch.ones(1))
        self.bias = nn.Parameter(torch.Tensor(1.0 * torch.ones(1, 4, 1, 1)).cuda())

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_uniform_(m.weight, mode='fan_in', nonlinearity='leaky_relu')
                if m.bias is not None:
                    fan_in, _ = nn.init._calculate_fan_in_and_fan_out(m.weight)
                    bound = 1 / math.sqrt(fan_in)
                    nn.init.uniform_(m.bias, -bound, bound)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.uniform_(m.weight)
                nn.init.zeros_(m.bias)

    def forward(self, search, kernel=None, memory_kernel=None,
                memory_confidence=None, cls_x_store=None):

        if kernel is not None:
            # Since template kernel (pooled by PrPool from the template patch) is inputted,
            #         we should conduct tracking with offline Siamese module

            # Encode feature for correlation first
            cls_z, cls_x = self.cls_encode(kernel, search)   # [z11, z12, z13]
            reg_z, reg_x = self.reg_encode(kernel, search)  # [x11, x12, x13]

            # DW for cls and reg
            cls_dw = self.cls_dw(cls_z, cls_x)
            reg_dw = self.reg_dw(reg_z, reg_x)
            x_reg = self.bbox_tower(reg_dw)
            x_bbox = self.adjust * self.bbox_pred(x_reg) + self.bias
            x_bbox = torch.exp(x_bbox)

            # Cls tower
            c = self.cls_tower(cls_dw)
            cls = 0.1 * self.cls_pred(c)

            if memory_kernel is None:
                # cache cls_x to prevent double processing of search area feature
                torch.cuda.empty_cache()
                return x_bbox, cls, cls_x, reg_x, None

        # Cls memory
        if memory_kernel is not None:

            # Since memory queue is inputted, we should conduct tracking with online memory module
            if cls_x_store is None:
                cls_mem_zs, cls_x_store = self.cls_encode(memory_kernel, x=search)
            else:
                # Use the cached cls_x to prevent double processing of search area feature
                cls_mem_zs, _ = self.cls_encode(memory_kernel, x=None)

            # Multi-scale correlation, between the memory queue and the search area feature in the template frame
            # batch, mem_size = memory_confidence.shape
            store_repeat = []
            for cls_x in cls_x_store:
                _, c, h, w = cls_x.shape
                cls_x_rep = cls_x.view(1, 1, c, h, w)
                cls_x_rep = cls_x_rep.repeat(1, 1, 1, 1, 1).view(-1, c, h, w)
                store_repeat.append(cls_x_rep)

            cls_mem_dw = self.cls_dw(cls_mem_zs, store_repeat)
            _, c, h, w = cls_mem_dw.shape
            cls_mem_dw = cls_mem_dw.view(1, 1, c, h, w)

            # Fuse memory correlation maps
            # cls_mem_fusion = self.conf_fusion(cls_mem_dw)
            cls_mem_fusion = cls_mem_dw.sum(dim=1)

            # Memory cls head
            # c_mem = self.cls_memory_tower(cls_mem_fusion)
            # cls_mem = 0.1 * self.cls_memory_pred(c_mem)
            c_mem = self.cls_tower(cls_mem_fusion)
            cls_mem = 0.1 * self.cls_pred(c_mem)

            if kernel is not None:
                torch.cuda.empty_cache()
                return x_bbox, cls, cls_x, reg_x, cls_mem
            else:
                torch.cuda.empty_cache()
                return None, None, None, None, cls_mem
        torch.cuda.empty_cache()
        return None


class AdjustLayer(nn.Module):
    def __init__(self, in_channels, out_channels, pr_pool=False):
        super(AdjustLayer, self).__init__()
        self.downsample = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels),
            )
        if pr_pool:
            self.prpooling = PrRoIPool2D(7, 7, spatial_scale=1.0)

    def forward(self, x, crop=False, pr_pool=False, bbox=None):

        # x_ori = self.downsample(x)
        # x_ori = self.upsampler(x)
        x_ori = x

        if not crop:
            return x_ori

        if crop:
            l = 4
            r = -4
            xf = x_ori[:, :, l:r, l:r]

        if not pr_pool:
            return x_ori, xf
        else:
            # PrPool feature according to pseudo bbox
            batch_index = torch.arange(0, x.shape[0]).view(-1, 1).float().to(x.device)
            bbox = torch.cat((batch_index, bbox), dim=1)

            xf_pr = self.prpooling(x_ori, bbox)
            return x_ori, xf_pr


class OverlapPatchEmbed(nn.Module):
    def __init__(self, patch_size=7, stride=4, in_chans=3, embed_dim=768):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        self.patch_size = patch_size
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride, padding=(patch_size[0]//2, patch_size[1]//2))
        self.norm = nn.LayerNorm(embed_dim)
        self.apply(self._init_weights)
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight,1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //=m.groups
            m.weight.data.normal_(0, math.sqrt(2.0/fan_out))
            if m.bias is not None:
                m.bias.data.zero_()
    def forward(self, x):
        x = self.proj(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)
        return x, H, W
