"""
    Python (C)VAE implementations
    Maurits Diephuis
"""

from __future__ import print_function
import numpy as np
import time
import argparse
import torch
import torch.nn as nn
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.optim import Adam
from torch.nn import functional as F
from torch.nn import init
from torchvision import datasets, transforms
from torchvision.utils import save_image
from torch.utils.data import DataLoader
from visdom import Visdom

from vae_models import CVAE

from nn_helpers.losses import loss_bce_kld, EarlyStopping
from nn_helpers.utils import one_hot_np


parser = argparse.ArgumentParser(description='VAE example')

# Task parameters


# Model parameters

parser.add_argument('--conditional', action='store_true', default=True,
                    help='Enable CVAE')

# Optimizer
parser.add_argument('--optimizer', type=str, default="adam",
                    help='Optimizer (default: Adam')
parser.add_argument('--batch-size', type=int, default=128, metavar='N',
                    help='input training batch-size')
parser.add_argument('--epochs', type=int, default=25, metavar='N',
                    help='number of training epochs')

parser.add_argument('--lr', type=float, default=1e-4,
                    help='Learning rate (default: 1e-4')

# Visdom / tensorboard
parser.add_argument('--visdom-url', type=str, default=None,
                    help='visdom url, needs http, e.g. http://localhost (default: None)')
parser.add_argument('--visdom-port', type=int, default=8097,
                    help='visdom server port (default: 8097')
parser.add_argument('--log-interval', type=int, default=1, metavar='N',
                    help='batch interval for logging (default: 1')

# Device (GPU)
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables cuda (default: False')

parser.add_argument('--ngpu', type=int, default=1,
                    help='Number of gpus available (default: 1)')

parser.add_argument('--seed', type=int, default=None,
                    help='Seed for numpy and pytorch (default: None')


args = parser.parse_args()

args.cuda = not args.no_cuda and torch.cuda.is_available()
use_visdom = args.visdom_url is not None


# Handle randomization


# Enable CUDA, set tensor type and device
if args.cuda:
    dtype = torch.cuda.FloatTensor
    device = torch.device("cuda:0")
    print('GPU')
else:
    dtype = torch.FloatTensor
    device = torch.device("cpu")


if args.seed is not None:
    print('Seed: {}'.format(args.seed))
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)


# Visdom hooks
def update_plot(win_title, y, x, opts={}):
    '''
    Update vidomplot by win_title. If it doesn't exist, create a new plot
    - win_title: name and title of plot
    - y: y coord
    - x: x coord
    - options_dict, example {'legend': 'NAME', 'ytickmin': 0, 'ytickmax': 1}
    '''
    if not viz.win_exists(win_title):
        viz.line(Y=np.array([y]), X=np.array([x]), win=win_title,
                 opts=opts)
    else:
        viz.line(Y=np.array([y]), X=np.array([x]), win=win_title,
                 update='append', opts=opts)


def reconstruction_example(model, device, dtype):
    model.eval()
    for _, (x, y) in enumerate(loader_val):
        x = x.type(dtype)
        x = x.to(device)
        y = torch.from_numpy(one_hot_np(y, 10))
        y = y.type(dtype)
        y = y.to(device)
        x_hat, _, _ = model(x, y)
        break

    x = x[:10].cpu().view(10 * 28, 28)
    x_hat = x_hat[:10].cpu().view(10 * 28, 28)
    comparison = torch.cat((x, x_hat), 1).view(10 * 28, 2 * 28)
    return comparison


def latentspace_example(model, device):
    draw = torch.randn(10, 20, device=device)
    label = torch.eye(10, 10, device=device)
    sample = model.decode(draw, label).cpu().view(10, 1, 28, 28)
    return sample


# save checkpoint
def save_checkpoint(state, filename):
    torch.save(state, filename)


# Training and Validation Dataloaders
kwargs = {'num_workers': 1, 'pin_memory': True} if args.cuda else {}

loader_train = DataLoader(
    datasets.MNIST('data', train=True, download=True,
                   transform=transforms.ToTensor()),
    batch_size=args.batch_size, shuffle=True, **kwargs)

loader_val = DataLoader(
    datasets.MNIST('data', train=False, transform=transforms.ToTensor()),
    batch_size=args.batch_size, shuffle=True, **kwargs)


def init_weights(m):
    if isinstance(m, nn.Linear) or isinstance(m, nn.ConvTranspose2d):
        init.xavier_uniform_(m.weight.data)



def get_optimizer(model):
    lr = 1e-3
    beta1 = 0.9
    beta2 = 0.999
    optimizer = Adam(model.parameters(), lr=lr,
                     betas=(beta1, beta2))
    return optimizer


def execute_graph(model, conditional, loader_train, loader_val,
                  loss_fn, scheduler, use_visdom):
    # Training loss
    t_loss = train_validate(model, loader_train, loss_bce_kld, scheduler, conditional, train=True)

    # Validation loss
    v_loss = train_validate(model, loader_val, loss_bce_kld, scheduler, conditional, train=False)

    # Step the scheduler based on the validation loss
    scheduler.step(v_loss)

    print('====> Epoch: {} Average Train loss: {:.4f}'.format(
          epoch, t_loss))
    print('====> Epoch: {} Average Validation loss: {:.4f}'.format(
          epoch, v_loss))

    if use_visdom:
        # Visdom: update training and validation loss plots
        update_plot('tloss', y=t_loss, x=epoch,
                    opts=dict(title='Training loss'))

        update_plot('vloss', y=v_loss, x=epoch,
                    opts=dict(title='Validation loss'))

        # Visdom: Show generated images
        sample = latentspace_example(model, device)
        sample = sample.detach().numpy()
        viz.images(sample, win='gen',
                   opts=dict(title='Generated sample ' + str(epoch)))

        # Visdom: Show example reconstruction
        comparison = reconstruction_example(model, device, dtype)
        viz.images(comparison.detach().numpy(), win='recon',
                   opts=dict(title='Reconstruction ' + str(epoch)))

    return v_loss


def train_validate(model, loader_data, loss_fn, scheduler, conditional, train):
    model.train() if train else model.eval()
    loss = 0
    batch_sz = len(loader_data.dataset)
    for batch_idx, (x, y) in enumerate(loader_data):
        x = x.to(device)
        if train:
            opt.zero_grad()
        if conditional:
            # use new function
            y = torch.from_numpy(one_hot_np(y, 10))
            # refractor out
            y = y.type(dtype)
            y = y.to(device)
            x_hat, mu, log_var = model(x, y)
        else:
            x_hat, mu, log_var = model(x)
        loss = loss_fn(x, x_hat, mu, log_var)

        loss += loss.item()

        if train:
            loss.backward()
            opt.step()
    # collect better stats
    return loss / batch_sz


"""
Visdom init
"""
if use_visdom:
    env_name = 'VAE'
    viz = Visdom(env=env_name)
    startup_sec = 2
    while not viz.check_connection() and startup_sec > 0:
        time.sleep(0.1)
        startup_sec -= 0.1
    # assert viz.check_connection(), 'Visdom connection failed'

"""
Run a conditional one-hot VAE
"""
model = CVAE().type(dtype)
model.apply(init_weights)
opt = get_optimizer(model)
scheduler = ReduceLROnPlateau(opt, 'min', verbose=True)
early_stopping = EarlyStopping('min', 0.0005, 5)

num_epochs = args.epochs
conditional = True
best_loss = np.inf
# Main training and validation loop

for epoch in range(1, num_epochs + 1):
    v_loss = execute_graph(model, conditional, loader_train, loader_val,
                           loss_bce_kld, scheduler, use_visdom)

    stop = early_stopping.step(v_loss)

    if v_loss < best_loss or stop:
        best_loss = v_loss
        print('Writing model checkpoint')
        save_checkpoint({
                        'epoch': epoch + 1,
                        'state_dict': model.state_dict(),
                        'val_loss': v_loss
                        },
                        'models/CVAE_{:04.4f}.pt'.format(v_loss))
    if stop:
        print('Early stopping at epoch: {}'.format(epoch))
        break

# Write a final sample to disk
sample = latentspace_example(model, device)
save_image(sample, 'output/sample_' + str(num_epochs) + '.png')

# Make a final reconstrunction, and write to disk
comparison = reconstruction_example(model, device, dtype)
save_image(comparison, 'output/comparison_' + str(num_epochs) + '.png')
