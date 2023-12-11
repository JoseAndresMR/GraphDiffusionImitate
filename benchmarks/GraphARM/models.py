import torch
from torch import nn
from torch_geometric.nn import GAT
from torch_geometric.utils import add_self_loops, degree
from torch.nn import functional as F
from torch.nn import Linear, ReLU
import math
from torch_geometric.nn import MessagePassing

class DiffusionOrderingNetwork(nn.Module):
    '''
    at each diffusion step t, we sample from this network to select a node 
    v_sigma(t) to be absorbed and obtain the corresponding masked graph Gt
    '''
    def __init__(self,
                 node_feature_dim,
                 num_node_types,
                 num_edge_types,
                 num_layers=3,
                 out_channels=1,
                 num_heads=6):
        super(DiffusionOrderingNetwork, self).__init__()

        num_node_types += 1 # add one for masked node type
        num_edge_types += 2 # add one for masked edge type and one for empty edge type
        

        # add positional encodings into node features
        self.embedding = nn.Embedding(num_embeddings=num_node_types, embedding_dim=node_feature_dim)

        self.gat = GAT(
            in_channels=node_feature_dim,
            out_channels=node_feature_dim,
            hidden_channels=num_heads * 6,
            num_layers=num_layers,
            dropout=0,
            heads=num_heads,
            residual=True
        )


    def positionalencoding(self, lengths, permutations):
        '''
        From Chen, et al. 2021 (Order Matters: Probabilistic Modeling of Node Sequences for Graph Generation)
        '''
        # length = sum([len(perm) for perm in permutations])
        l_t = len(permutations[0])
        # pes = [torch.zeros(length, self.d_model) for length in lengths]
        pes = torch.split(torch.zeros((sum(lengths), self.d_model), device=self.device), lengths)
        position = torch.arange(0, l_t, device=self.device).unsqueeze(1) + 1
        div_term = torch.exp((torch.arange(0, self.d_model, 2, dtype=torch.float, device=self.device) *
                              -(math.log(10000.0) / self.d_model)))
        # test = torch.sin(position.float() * div_term)
        for i in range(len(lengths)):
            pes[i][permutations[i], 0::2] = torch.sin(position.float() * div_term)
            pes[i][permutations[i], 1::2] = torch.cos(position.float() * div_term)

        pes = torch.cat(pes)
        return pes

    def forward(self, G, p=None):

        h = self.embedding(G.x.squeeze().long())

        h = self.gat(h, G.edge_index.long(), edge_attr=G.edge_attr.long())

        # TODO augment node features with positional encodings
        # if p is not None:
        #     # p = self.positionalencoding(G.batch_num_nodes().tolist(), p) original from Chen et al.
        #     p = self.positionalencoding(G.x.shape[0], p)
        #     h = h + p
        
        # softmax over nodes
        h = F.softmax(h, dim=0)
        
        return h # outputs probabilities for a categorical distribution over nodes
    
    
class MPLayer(MessagePassing):
    '''
    Custom message passing layer for the GraphARM model
    '''
    def __init__(self, in_channels, out_channels):
        super().__init__(aggr='sum') #  "Max" aggregation.
        self.f = nn.Sequential(Linear(3 * in_channels, out_channels),
                       nn.ReLU(),
                       Linear(out_channels, out_channels)) # MLP for message construction
        self.g = nn.Sequential(Linear(3 * in_channels, out_channels),
                          nn.ReLU(),
                          Linear(out_channels, out_channels)) # MLP for attention coefficients
        
        self.gru = nn.GRU(2*out_channels, out_channels)
        
    def forward(self, x, edge_index, edge_attr):
        # x has shape [N, in_channels]
        # edge_index has shape [2, E]

        # edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))
        out = self.propagate(edge_index, x=x, edge_attr=edge_attr)
        out, _ = self.gru(torch.cat([x, out], dim=-1)) # discard final hidden state
        return out

    def message(self, x_i, x_j, edge_attr):
        # x_i has shape [E, in_channels]
        # x_j has shape [E, in_channels]

        h_vi = x_i
        h_vj = x_j
        h_eij = edge_attr

        m_ij = self.f(torch.cat([h_vi, h_vj, h_eij], dim=-1))
        a_ij = self.g(torch.cat([h_vi, h_vj, h_eij], dim=-1))
        return m_ij * a_ij


class DenoisingNetwork(nn.Module):
    def __init__(self,
                node_feature_dim,
                edge_feature_dim,
                num_node_types,
                num_edge_types,
                out_channels,
                num_layers=5,
                hidden_dim=256,
                K=20):
        super().__init__()
        num_edge_types += 1 # add one for empty edge type
        self.K = K
        self.num_layers = num_layers
        self.node_embedding = Linear(node_feature_dim, hidden_dim)
        self.edge_embedding = Linear(edge_feature_dim, hidden_dim)

        self.layers = nn.ModuleList()
        for i in range(num_layers):
            self.layers.append(MPLayer(hidden_dim, hidden_dim))

        self.mlp_alpha = nn.Sequential(Linear(3*hidden_dim, hidden_dim),
                                       nn.ReLU(),
                                       Linear(hidden_dim, self.K))
        
        self.node_pred_layer = nn.Sequential(Linear(2*hidden_dim, hidden_dim),
                                       nn.ReLU(),
                                       Linear(hidden_dim, num_node_types))
        
        self.edge_pred_layer = nn.Sequential(Linear(hidden_dim, hidden_dim),
                                       nn.ReLU(),
                                       Linear(hidden_dim, num_edge_types*K))


        
    def forward(self, x, edge_index, edge_attr, v_t=None):
        # make sure x and edge_attr are of type float, for the MLPs
        x = x.float()
        edge_attr = edge_attr.float()

        h_v = self.node_embedding(x)
        h_e = self.edge_embedding(edge_attr.reshape(-1, 1))
        
        for l in range(self.num_layers):
            h_v = self.layers[l](h_v, edge_index, h_e)


        # graph-level embedding, from average pooling layer
        graph_embedding = torch.mean(h_v, dim=0)

        # repeat graph embedding to have the same shape as h_v
        graph_embedding = graph_embedding.repeat(h_v.shape[0], 1)

        node_pred = self.node_pred_layer(torch.cat([graph_embedding, h_v], dim=1)) # hidden_dim + 1
        # aggregate with torch mean pooling
        node_pred = torch.mean(node_pred, dim=0)

        
        # edge prediction follows a mixture of multinomial distribution, with
        # the Softmax(sum(mlp_alpha([graph_embedding, h_vi, h_vj])))
        alphas = torch.zeros(h_v.shape[0], self.K)
        if v_t is None:
            v_t = h_v.shape[0] - 1# node being masked, this assumes that the masked node is the last node in the graph
        h_v_t = h_v[v_t, :].repeat(h_v.shape[0], 1)

        alphas = self.mlp_alpha(torch.cat([graph_embedding, h_v_t, h_v], dim=1))

        alphas = F.softmax(torch.sum(alphas, dim=0, keepdim=True), dim=1)

        p_v = F.softmax(node_pred, dim=-1)
        log_theta = self.edge_pred_layer(h_v)
        log_theta = log_theta.view(h_v.shape[0], -1, self.K) # h_v.shape[0] is the number of steps (nodes) (block size)
        p_e = torch.sum(alphas * F.softmax(log_theta, dim=1), dim=-1) # softmax over edge types

        return p_v, p_e