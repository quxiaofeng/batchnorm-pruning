''' https://arxiv.org/pdf/1802.00124v1.pdf '''
from __future__ import print_function

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

import torchvision
import torchvision.transforms as transforms

from tensorboardX import SummaryWriter, FileWriter

import os
import argparse

from utils import *
from torch.autograd import Variable

import sgd as bnopt

from models import *

from models.layers import bn

'''
Equation (2) on page 6
'''
def compute_penalties(model, rho, image_dim=40):
    penalties  = []
    image_dims = compute_dims(model) # calculate output sizes of each convolution so we can count penalties

    # only considering conv layers with batchnorm
    layers = list(filter(lambda l : isinstance(l, nn.Conv2d), expand_model(model, [])))

    # zip xs (tail xs) - need to know kernel size of follow-up layer
    for i in range(len(layers)):
        l    = layers[i]
        tail = layers[i+1:]

        i_w, i_h = image_dim, image_dim
        k_w, k_h = l.kernel_size[0], l.kernel_size[1]
        c_prev   = l.in_channels
        c_next   = l.out_channels

        follow_up_cost = 0.

        for j, follow_up_conv in enumerate(tail):
            follow_up_cost += follow_up_conv.kernel_size[0] * follow_up_conv.kernel_size[1] * follow_up_conv.in_channels + image_dims[j+i]**2

        ista = ((1 / i_w * i_h) * (k_w * k_h * c_prev + follow_up_cost))
        ista = rho * ista

        print(ista)
        penalties.append(ista)

    return penalties


'''
An alternative implementation where only the direct follow up conv is
included in the calculation of ISTA penalties.
'''
def compute_penalties_(model, rho, image_dim=40):
    penalties  = []
    image_dims = compute_dims(model)

    layers = expand_model(model, [])

    for l1, l2 in zip(layers,layers[1:]):
        if(isinstance(l1, nn.Conv2d) and isinstance(l2, bn.BatchNorm2dEx)):
            # get a count of the zero-valued weights in l2
            # subtract count from follow_up_conv.in_channels
            c_next = l1.out_channels - num_zeros

            for j, follow_up_conv in enumerate(tail):
                follow_up_cost += follow_up_conv.kernel_size[0] * follow_up_conv.kernel_size[1] * c_next + image_dims[j+i]**2
                c_next = follow_up_conv.in_channels

    return penalties



def scale_gammas(alpha, model, scale_down=True):
    # get pairs of consecutive layers
    layers = expand_model(model, [])

    alpha_ = 1 / alpha

    if not scale_down:
        # after training we want to scale back up so need to invert alpha
        alpha_  = alpha
        alpha   = 1 / alpha

    for l1, l2 in zip(layers,layers[1:]):
        if(isinstance(l1, bn.BatchNorm2dEx) and isinstance(l2, nn.Conv2d)):
            l1.weight.data = l1.weight.data * alpha
            l2.weight.data = l2.weight.data * alpha_

    return model


def switch_to_follow(model):
    first = True # want to skip the first bn layer - only do follow up layers
    for layer in expand_model(model, []):
        if isinstance(layer, bn.BatchNorm2dEx):
            if not first:
                layer.follow = True
            first = False

def train_model(model_name, model_weights, ista_penalties, num_epochs):

    best_acc = 0.
    learning_rate = 0.01

    non_bn_params = [p for n, p in model.named_parameters() if 'bnx' not in n]
    bn_params     = [p for n, p in model.named_parameters() if 'bnx' in n]

    # should weight decay be zero?
    optimizer    = optim.SGD([p for n, p in model.named_parameters() if 'bnx' not in n], lr=learning_rate, momentum=0.9, weight_decay=5e-4)
    bn_optimizer = bnopt.BatchNormSGD([p for n, p in model.named_parameters() if 'bnx' in n], lr=learning_rate, ista=ista_penalties, momentum=0.9)

    for epoch in range(1,num_epochs):
        train(model_weights, epoch, writer, "train", optimizer, bn_optimizer, train_loader)
        best_acc = test(model_name, model_weights, epoch, writer, "train", test_loader, best_acc)
        count_sparse_bn(model_weights, writer, epoch)
        spbns = print_sparse_bn(model_weights)

        for name, param in model_weights.named_parameters():
            writer.add_histogram(name, param.clone().cpu().data.numpy(), epoch)

        #print(spbns)
        #writer.add_histogram("sparsity", spbns, epoch)
    return best_acc


def finetune(model, writer, epochs):
    best_acc = 0.
    optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9, weight_decay=5e-4)
    for epoch in range(1, epochs):
        train(model, epoch, writer,"finetune", optimizer, bn_optimizer=None, trainloader=train_loader, finetune=True)
        best_acc = test(model_name, model, epoch, writer,"finetune", test_loader, best_acc)
        count_sparse_bn(model, writer, epoch)
        print_sparse_bn(model)



parser = argparse.ArgumentParser(description="Rethinking Smaller Norm in Channel Pruning")
parser.add_argument('--pretrained', action='store_true', help='Please provide path to pretrained model')
args = parser.parse_args()

if __name__=='__main__':
    train_loader, test_loader = get_data()

    writer = SummaryWriter()

    # get the model
    model = VGG16()
    model_name = "VGG-16"
    compressed_model = VGG16Compressed

    initial_training_epochs = 200
    finetuning_epochs       = 50
    compress_epochs         = 10

    # fixed hyperparams for now - need to add parsing support
    alpha = 1.
    rho   = 0.0000001

    # step one: compute ista penalties
    ista_penalties = compute_penalties(model, rho)
    print_layer_ista_pair(model, ista_penalties)

    # step two: gamma rescaling trick
    #scale_gammas(alpha, model=model, scale_down=True)

    count_sparse_bn(model, writer, 0)
    print_sparse_bn(model)

    # step three: end-to-end-training
    train_model(model_name=model_name, model_weights=model, ista_penalties=ista_penalties, num_epochs=initial_training_epochs)

    # step four: remove constant channels by switching bn to "follow" mode
    switch_to_follow(model)

    # step five: gamma rescaling trick
    #scale_gammas(alpha, model=model, scale_down=False)

    # step six: finetune
    finetune(model, writer, finetuning_epochs)

    ##### Remove all unnecessary channels
    model_name = model_name + "Compressed"

    # zero out any channels that have a 0 batchnorm weight
    print("Compressing model...")
    sparsify_on_bn(model)

    new_model = compress_convs(model, compressed_model)

    # step six: finetune
    finetune(new_model, writer, compress_epochs)

    writer.export_scalars_to_json("./all_scalars.json")
    writer.close()
