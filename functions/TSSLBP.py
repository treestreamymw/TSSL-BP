import torch
import torch.nn as nn
import torch.nn.functional as f
from time import time 
import global_v as glv


def psp(inputs, network_config):
    shape = inputs.shape
    n_steps = network_config['n_steps']
    tau_s = network_config['tau_s']

    syn = torch.zeros((shape[0], shape[1], shape[2], shape[3]), dtype=glv.dtype).to(glv.device)
    syns = torch.zeros((shape[0], shape[1], shape[2], shape[3], n_steps), dtype=glv.dtype).to(glv.device)

    for t in range(n_steps):
        syn = syn - syn / tau_s + inputs[..., t]
        syns[..., t] = syn / tau_s

    return syns


class PSP_spike_large_batch(torch.autograd.Function):  # a and u is the incremnet of each time steps
    """
    Here we implement our spiking nonlinearity which also implements
    the surrogate gradient. By subclassing torch.autograd.Function,
    we will be able to use all of PyTorch's autograd functionality.
    """

    @staticmethod
    def forward(ctx, inputs, network_config, layer_config):
        shape = inputs.shape
        n_steps = network_config['n_steps']
        theta_m = 1/network_config['tau_m']
        theta_s = 1/network_config['tau_s']
        threshold = layer_config['threshold']

        mem = torch.zeros((shape[0], shape[1], shape[2], shape[3]), dtype=glv.dtype).to(glv.device)
        syn = torch.zeros((shape[0], shape[1], shape[2], shape[3]), dtype=glv.dtype).to(glv.device)
        ref = torch.zeros((shape[0], shape[1], shape[2], shape[3], shape[4]), dtype=glv.dtype).to(glv.device)
        delta_refs = torch.zeros((shape[0], shape[1], shape[2], shape[3], shape[4], shape[4]), dtype=glv.dtype).to(glv.device)
        mems = []
        mem_updates = []
        outputs = []
        syns_posts = []
        outputs = []
        for t in range(n_steps):
            mem_update = (-theta_m) * mem + inputs[..., t]
            delta_ref = (-theta_m) * ref
            mem += mem_update
            ref += delta_ref

            out = mem > threshold
            out = out.type(glv.dtype)
            mems.append(mem)
            if t > 0:
                out_tmp = out.unsqueeze(-1).repeat(1, 1, 1, 1, t)
                ref[..., 0:t] *= (1-out_tmp)
                delta_ref[..., 0:t] *= out_tmp
            ref[..., t] = (-1) * mem * out
            delta_refs[..., 0:t, t] = delta_ref[..., 0:t]

            mem = mem * (1-out)
            outputs.append(out)
            mem_updates.append(mem_update)

            syn = syn + (out - syn) * theta_s
            syns_posts.append(syn)
        mems = torch.stack(mems, dim = 4)
        mem_updates = torch.stack(mem_updates, dim = 4)
        syns_posts = torch.stack(syns_posts, dim = 4)
        outputs = torch.stack(outputs, dim = 4)
        ctx.save_for_backward(mem_updates, outputs, mems, delta_refs, torch.tensor([threshold]))
        return syns_posts

    @staticmethod
    def backward(ctx, grad_delta):
        # in: grad_output: e(l-1)
        # out: grad: delta(l-1)
        """
        In the backward pass we receive a Tensor we need to compute the
        surrogate gradient of the loss with respect to the input.
        """
        (delta_u, outputs, u, delta_refs, others) = ctx.saved_tensors
        start_time = time()
        shape = outputs.shape
        n_steps = glv.n_steps
        threshold = others[0].item()

        mini_batch = 5
        partial_a_inter = glv.partial_a.repeat(mini_batch, shape[1], shape[2], shape[3], 1, 1)
        grad_a = torch.empty_like(delta_u)

        for i in range(int(shape[0]/mini_batch)):
            # part two, intra-neuron: effect of reset
            delta_refs_batch = delta_refs[i*mini_batch:(i+1)*mini_batch, ...]
            partial_a_intra = torch.einsum('...ij, ...jk -> ...ik', partial_a_inter, delta_refs_batch)

            # part one, inter-neuron + part two, intra-neuron
            partial_a_all = partial_a_inter + partial_a_intra

            grad_a[i*mini_batch:(i+1)*mini_batch, ...] = torch.einsum('...ij, ...j -> ...i', partial_a_all, grad_delta[i*mini_batch:(i+1)*mini_batch, ...])

        if torch.sum(outputs)/(shape[0] * shape[1] * shape[2] * shape[3] * shape[4]) > 0.1:
            partial_u = torch.clamp(1 / delta_u, -10, 10) * outputs
            grad = grad_a * partial_u
        else:
            # computing partial a / partial u
            a = 0.2
            f = torch.clamp((-1 * u + threshold) / a, -8, 8)
            f = torch.exp(f)
            f = f / ((1 + f) * (1 + f) * a)

            grad = grad_a * f
        return grad, None, None, None, None, None, None, None, None


class PSP_spike_long_time(torch.autograd.Function):  # a and u is the incremnet of each time steps
    @staticmethod
    def forward(ctx, inputs, network_config, layer_config):
        shape = inputs.shape
        n_steps = network_config['n_steps']
        theta_m = 1/network_config['tau_m']
        theta_s = 1/network_config['tau_s']
        threshold = layer_config['threshold']

        mem = torch.zeros((shape[0], shape[1], shape[2], shape[3]), dtype=glv.dtype).to(glv.device)
        syn = torch.zeros((shape[0], shape[1], shape[2], shape[3]), dtype=glv.dtype).to(glv.device)
        mems = []
        mem_updates = []
        outputs = []
        syns_posts = []
        outputs = []
        for t in range(n_steps):
            mem_update = (-theta_m) * mem + inputs[..., t]
            mem += mem_update

            out = mem > threshold
            out = out.type(glv.dtype)
            mems.append(mem)
            mem = mem * (1-out)
            outputs.append(out)
            mem_updates.append(mem_update)

            syn = syn + (out - syn) * theta_s
            syns_posts.append(syn)
        mems = torch.stack(mems, dim = 4)
        mem_updates = torch.stack(mem_updates, dim = 4)
        syns_posts = torch.stack(syns_posts, dim = 4)
        outputs = torch.stack(outputs, dim = 4)
        ctx.save_for_backward(mem_updates, outputs, mems, torch.tensor([threshold]))
        return syns_posts

    @staticmethod
    def backward(ctx, grad_delta):
        (delta_u, outputs, u, others) = ctx.saved_tensors
        start_time = time()
        shape = outputs.shape
        n_steps = glv.n_steps
        threshold = others[0].item()

        # part two, intra-neuron: effect of reset
        # partial_u_extend = partial_u.unsqueeze(-1).repeat(1, 1, 1, 1, 1, n_steps)
        # partial_a_2 = refs * partial_u_extend.transpose(4, 5)
        # partial_a_2 = torch.einsum('...ij, ...jk -> ...ik', partial_a_2, partial_a)
        partial_a_tmp = glv.partial_a[..., 0, :].repeat(shape[0], shape[1], shape[2], shape[3], 1)
        grad_a = torch.empty_like(delta_u)
        for t in range(n_steps):
            grad_a[..., t] = torch.sum(partial_a_tmp[..., 0:n_steps-t]*grad_delta[..., t:n_steps], dim=4) 

        if torch.sum(outputs)/(shape[0] * shape[1] * shape[2] * shape[3] * shape[4]) > 0.1:
            # part one, inter-neuron
            partial_u = torch.clamp(1 / delta_u, -10, 10) * outputs
            grad = grad_a * partial_u
        else:
            # computing partial a / partial u
            a = 0.2
            f = torch.clamp((-1 * u + threshold) / a, -8, 8)
            f = torch.exp(f)
            f = f / ((1 + f) * (1 + f) * a)

            grad = grad_a * f
        return grad, None, None, None, None, None, None, None, None
    
