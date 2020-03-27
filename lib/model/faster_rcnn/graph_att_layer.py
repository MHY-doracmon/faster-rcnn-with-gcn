"""
Copyright (c) Microsoft Corporation.
Licensed under the MIT license.

Relation-aware Graph Attention Network for Visual Question Answering
Linjie Li, Zhe Gan, Yu Cheng, Jingjing Liu
https://arxiv.org/abs/1903.12314

This code is written by Linjie Li.
"""
import torch
import torch.nn as nn
from model.faster_rcnn.fc import FCNet
import math
from torch.nn.utils.weight_norm import weight_norm


class GraphSelfAttentionLayer(nn.Module):
    def __init__(self, feat_dim, nongt_dim=20, pos_emb_dim=-1,
                 num_heads=16, dropout=[0.2, 0.5]):
        """ Attetion module with vectorized version

        Args:
            position_embedding: [num_rois, nongt_dim, pos_emb_dim]
                                used in implicit relation
            pos_emb_dim: set as -1 if explicit relation
            nongt_dim: number of objects consider relations per image
            fc_dim: should be same as num_heads
            feat_dim: dimension of roi_feat
            num_heads: number of attention heads
        Returns:
            output: [num_rois, ovr_feat_dim, output_dim]
        """
        super(GraphSelfAttentionLayer, self).__init__()
        # multi head
        self.fc_dim = num_heads
        self.feat_dim = feat_dim
        self.dim = (feat_dim, feat_dim, feat_dim)
        self.dim_group = (int(self.dim[0] / num_heads),
                          int(self.dim[1] / num_heads),
                          int(self.dim[2] / num_heads))
        self.num_heads = num_heads
        self.pos_emb_dim = pos_emb_dim
        if self.pos_emb_dim > 0:
            self.pair_pos_fc1 = FCNet([pos_emb_dim, self.fc_dim], None, dropout[0])
        self.query = FCNet([feat_dim, self.dim[0]], None, dropout[0])
        self.nongt_dim = nongt_dim

        self.key = FCNet([feat_dim, self.dim[1]], None, dropout[0])

        self.linear_out_ = weight_norm(
                            nn.Conv2d(in_channels=self.fc_dim * feat_dim,
                                      out_channels=self.dim[2],
                                      kernel_size=(1, 1),
                                      groups=self.fc_dim), dim=None) # 每一个num_heads通道之间进行group卷积，

    def forward(self, roi_feat, adj_matrix,
                position_embedding, label_biases_att):
        """
        Args:
            roi_feat: [batch_size, N, feat_dim]
            adj_matrix: [batch_size, N, nongt_dim]
            position_embedding: [batch_size, num_rois, nongt_dim, pos_emb_dim]
            label_biases_att: [batch_size, N, nongt_dim]
        Returns:
            output: [batch_size, num_rois, ovr_feat_dim, output_dim]
        """
        batch_size = roi_feat.size(0)
        num_rois = roi_feat.size(1)
        # nongt_dim: number of objects consider relations per image
        nongt_dim = self.nongt_dim if self.nongt_dim < num_rois else num_rois
        # [batch_size,nongt_dim, feat_dim]
        nongt_roi_feat = roi_feat[:, :nongt_dim, :] # 只考虑到 nongt_dim 个proposal的特征

        # [batch_size,num_rois, self.dim[0] = feat_dim]
        q_data = self.query(roi_feat)

        # [batch_size,num_rois, num_heads, feat_dim /num_heads]
        q_data_batch = q_data.view(batch_size, num_rois, self.num_heads,
                                   self.dim_group[0])

        # [batch_size,num_heads, num_rois, feat_dim /num_heads]
        q_data_batch = torch.transpose(q_data_batch, 1, 2)

        # [batch_size,nongt_dim, self.dim[1] = feat_dim]
        k_data = self.key(nongt_roi_feat)

        # [batch_size,nongt_dim, num_heads, feat_dim /num_heads]
        k_data_batch = k_data.view(batch_size, nongt_dim, self.num_heads,
                                   self.dim_group[1])

        # [batch_size,num_heads, nongt_dim, feat_dim /num_heads]
        k_data_batch = torch.transpose(k_data_batch, 1, 2)

        # [batch_size,nongt_dim, feat_dim]
        v_data = nongt_roi_feat

        # [batch_size,num_heads, num_rois, feat_dim /num_heads] *  # [batch_size,num_heads, nongt_dim, feat_dim /num_heads]
        # [batch_size, num_heads, num_rois, nongt_dim]
        aff = torch.matmul(q_data_batch, torch.transpose(k_data_batch, 2, 3))

        # aff_scale, [batch_size, num_heads, num_rois, nongt_dim]
        aff_scale = (1.0 / math.sqrt(float(self.dim_group[1]))) * aff #相当于除以sqrt(d_k) 相当于归一化
        # aff_scale, [batch_size,num_rois,num_heads, nongt_dim]
        aff_scale = torch.transpose(aff_scale, 1, 2)
        weighted_aff = aff_scale # 相当于只考虑视觉特征的关系矩阵

        if position_embedding is not None and self.pos_emb_dim > 0:
            # Adding goemetric features
            position_embedding = position_embedding.float()
            # [batch_size,num_rois * nongt_dim, emb_dim]
            position_embedding_reshape = position_embedding.view(
                (batch_size, -1, self.pos_emb_dim))

            # position_feat_1, [batch_size,num_rois * nongt_dim, fc_dim]
            position_feat_1 = self.pair_pos_fc1(position_embedding_reshape) #维度变化  [batch_size,num_rois * nongt_dim, num_heads]
            position_feat_1_relu = nn.functional.relu(position_feat_1)

            # aff_weight, [batch_size,num_rois, nongt_dim, fc_dim]
            aff_weight = position_feat_1_relu.view(
                (batch_size, -1, nongt_dim, self.fc_dim))

            # aff_weight, [batch_size,num_rois, fc_dim, nongt_dim]
            aff_weight = torch.transpose(aff_weight, 2, 3)

            thresh = torch.FloatTensor([1e-6]).cuda()
            # weighted_aff, [batch_size,num_rois, fc_dim, nongt_dim]
            threshold_aff = torch.max(aff_weight, thresh) # 相当于一个clip操作

            weighted_aff += torch.log(threshold_aff) # 视觉关系矩阵加上位置关系矩阵

        # 因为adj_matrix 是全1矩阵，所以这部分代码没有意义
        if adj_matrix is not None:
            # weighted_aff_transposed, [batch_size,num_rois, nongt_dim, num_heads]
            weighted_aff_transposed = torch.transpose(weighted_aff, 2, 3)
            zero_vec = -9e15*torch.ones_like(weighted_aff_transposed)

            adj_matrix = adj_matrix.view(
                            adj_matrix.shape[0], adj_matrix.shape[1],
                            adj_matrix.shape[2], 1) #[bs, N, nongt_num. 1]
            adj_matrix_expand = adj_matrix.expand(
                                (-1, -1, -1,
                                 weighted_aff_transposed.shape[-1])) # [bs, N, nongt_num. num_heads]
            weighted_aff_masked = torch.where(adj_matrix_expand > 0,
                                              weighted_aff_transposed,
                                              zero_vec) # 根据开始求得邻接矩阵进行选择，如果大于0的就是计算的系数，如果不大于0就用0填充
# 这里为什么要加上label biases呢，就是因为如果邻接矩阵中如果本来存在连接，就加上一个bias，使得更高，但我感觉也没啥意义，
            weighted_aff_masked = weighted_aff_masked + label_biases_att.unsqueeze(3) # [bs, N, nongt_num, num_heads]
            weighted_aff = torch.transpose(weighted_aff_masked, 2, 3)  # [bs, N,  num_heads， nongt_num]

        # aff_softmax, [batch_size, num_rois, fc_dim, nongt_dim]
        aff_softmax = nn.functional.softmax(weighted_aff, 3) #对输入的特征做一个softmax进行归一化

        # aff_softmax_reshape, [batch_size, num_rois*fc_dim, nongt_dim]
        aff_softmax_reshape = aff_softmax.view((batch_size, -1, nongt_dim))

        # output_t, [batch_size, num_rois*fc_dim, nongt_dim]*[batch_size,nongt_dim, feat_dim]--》
        # [batch_size, num_rois * fc_dim, feat_dim]
        output_t = torch.matmul(aff_softmax_reshape, v_data) #将这 nongt_dim个proposal的特征进行聚合操作

        # output_t, [batch_size*num_rois, fc_dim * feat_dim, 1, 1]
        output_t = output_t.view((-1, self.fc_dim * self.feat_dim, 1, 1))

        # linear_out, [batch_size*num_rois, dim[2], 1, 1]
        linear_out = self.linear_out_(output_t) # 利用二维卷积的group卷积操作实现不同heads的信息聚合
        output = linear_out.view((batch_size, num_rois, self.dim[2])) #[bs, num_roi, feat_dim]
        return output