import argparse
import os
import torch
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from torch.utils.data import DataLoader

from dataloader import VideoClassificationDataset
from models.video_classifiers import NeXtVLADModel
from metrics import calculate_gap
from tqdm import tqdm

device = torch.device("cuda:0")


def train(opt, model, optimizer, scheduler, train_loader):
    with tqdm(total=len(train_loader)) as pb:
        for data in train_loader:
            fc_feats = data['fc_feats'].to(device)
            labels = data['ground_truth'].to(device)
            masks = data['mask'].to(device)

            out = model(fc_feats, mask=masks)
            loss = F.binary_cross_entropy(out, labels)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            str_loss = f"{loss.cpu().data.numpy():.4f}"
            pb.update(1)
            pb.set_postfix(epoch=epoch, loss=str_loss)


def eval(opt, model, test_loader):
    preds = []
    actuals = []

    for data in test_loader:
        fc_feats = data['fc_feats'].to(device)
        labels = data['ground_truth']
        masks = data['mask'].to(device)

        out = model(fc_feats, mask=masks)
        out = out.cpu().data.numpy()
        labels = labels.cpu().data.numpy()
        preds.extend(out)
        actuals.extend(labels)

    gap_score = calculate_gap(np.asarray(preds), np.asarray(actuals), top_k=opt['gapk'])
    return gap_score


if __name__ == '__main__':
    opt = argparse.ArgumentParser()
    opt.add_argument('train_feats_dir', help="Directory where train features are stored.")
    opt.add_argument('test_feats_dir', help="Directory where test features are stored.")
    opt.add_argument('--max_frames', help="Max frames length of dataset.", default=50, type=int)
    opt.add_argument('--gapk', help="Value of K for computing GAP score.", default=20, type=int)
    opt.add_argument('--num_epochs', help="Number of epochs.", default=5, type=int)
    opt.add_argument('--ckpt_dir', help="Where to save checkpoints.", default='ckpt/')

    opt = vars(opt.parse_args())

    if not os.path.isdir(opt['ckpt_dir']):
        os.mkdir(opt['ckpt_dir'])

    train_opts = {
        'feats_dir': opt['train_feats_dir'],
        'max_frames': opt['max_frames']
    }
    train_dataset = VideoClassificationDataset(train_opts, 'train')
    train_loader = DataLoader(train_dataset,
                              batch_size=8,
                              num_workers=4,
                              shuffle=True)

    test_opts = {
        'feats_dir': opt['test_feats_dir'],
        'max_frames': opt['max_frames']
    }
    test_dataset = VideoClassificationDataset(test_opts, 'test')
    test_loader = DataLoader(test_dataset,
                             batch_size=8,
                             num_workers=4,
                             shuffle=True)

    model = NeXtVLADModel(train_dataset.num_classes, max_frames=opt['max_frames'])
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    exp_lr_schedulr = optim.lr_scheduler.StepLR(optimizer, step_size=25)

    model.to(device)

    for epoch in range(opt['num_epochs']):
        model.train()
        train(opt, model, optimizer, exp_lr_schedulr, train_loader)

        model.eval()
        gap_score = eval(opt, model, test_loader)
        print(f"GAP({opt['gapk']}): {gap_score:.3f}")

        model_path = os.path.join(opt['ckpt_dir'], f"model_e{epoch}_gap{opt['gapk']}-{gap_score:.3f}.pth")
        torch.save(model.state_dict(), model_path)
        print(f"Model saved to {model_path}")