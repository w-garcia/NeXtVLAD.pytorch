import os
import torch
import numpy as np
import math
import torch.nn as nn
import pretrainedmodels

from PIL import Image
from pretrainedmodels.utils import ToRange255, ToSpaceBGR, transforms, munchify


def feature_pca_whiten(feat, center, eigenvals, eigenvecs):
    epsilon = 1e-4
    d = feat.shape[0]

    # subtract mean
    fcen = feat - center
    # principal components
    fpca = fcen.reshape((1, d)).dot(eigenvecs.T).squeeze(0)
    # whiten
    pcaw = fpca / np.sqrt(eigenvals + epsilon)

    return pcaw


def feature_pca(feat, center, eigenvals, eigenvecs):
    """
    Skip whitening, as done by Lin et al.
    :param feat:
    :param center:
    :param eigenvals:
    :param eigenvecs:
    :return:
    """
    d = feat.shape[0]

    # subtract mean
    fcen = feat - center
    # principal components
    fpca = fcen.reshape((1, d)).dot(eigenvecs.T).squeeze(0)

    return fpca


def create_batches(frames_to_do, tf_img_fn, logger=None, batch_size=32):
    n = len(frames_to_do)
    if n < batch_size:
        if logger: logger.warning("Sample size less than batch size: Cutting batch size.")
        batch_size = n

    if logger: logger.info("Generating {} batches...".format(n // batch_size))
    batches = []
    frames_to_do = np.array(frames_to_do)

    for idx in range(0, n, batch_size):
        frames_idx = list(range(idx, min(idx+batch_size, n)))
        batch_frames = frames_to_do[frames_idx]

        batch_tensor = None
        for i, frame_ in enumerate(batch_frames):
            if type(frame_) is np.ndarray:
                input_frame = Image.fromarray(frame_).convert('RGB')
            else: # filename
                input_frame = Image.open(frame_).convert('RGB')
            input_tensor = tf_img_fn(input_frame)  # 3x400x225 -> 3x299x299 size may differ
            # input_tensor = input_tensor.unsqueeze(0)  # 3x299x299 -> 1x3x299x299
            if batch_tensor is None:
                batch_tensor = torch.zeros((len(batch_frames),) + input_tensor.shape)
            batch_tensor[i] = input_tensor

        batch_ag = torch.autograd.Variable(batch_tensor, requires_grad=False)
        batches.append(batch_ag)
    return batches


class TransformImage(object):

    def __init__(self, opts, scale=0.875, random_crop=False,
                 random_hflip=False, random_vflip=False,
                 preserve_aspect_ratio=True):
        if type(opts) == dict:
            opts = munchify(opts)
        self.input_size = opts.input_size
        self.input_space = opts.input_space
        self.input_range = opts.input_range
        self.mean = opts.mean
        self.std = opts.std

        # https://github.com/tensorflow/models/blob/master/research/inception/inception/image_processing.py#L294
        self.scale = scale
        self.random_crop = random_crop
        self.random_hflip = random_hflip
        self.random_vflip = random_vflip

        tfs = []
        if preserve_aspect_ratio:
            tfs.append(transforms.Resize(int(math.floor(max(self.input_size)/self.scale))))
        else:
            height = int(self.input_size[1] / self.scale)
            width = int(self.input_size[2] / self.scale)
            tfs.append(transforms.Resize((height, width)))

        if random_crop:
            tfs.append(transforms.RandomCrop(max(self.input_size)))
        # else:
        #     tfs.append(transforms.CenterCrop(max(self.input_size)))

        if random_hflip:
            tfs.append(transforms.RandomHorizontalFlip())

        if random_vflip:
            tfs.append(transforms.RandomVerticalFlip())

        tfs.append(transforms.ToTensor())
        tfs.append(ToSpaceBGR(self.input_space=='BGR'))
        tfs.append(ToRange255(max(self.input_range)==255))
        tfs.append(transforms.Normalize(mean=self.mean, std=self.std))

        self.tf = transforms.Compose(tfs)

    def __call__(self, img):
        tensor = self.tf(img)
        return tensor


def init_model(gpu_ids, model_name):

    # model_name = 'pnasnet5large'
    # could be fbresnet152 or inceptionresnetv2
    model = pretrainedmodels.__dict__[model_name](num_classes=1000, pretrained='imagenet')
    model.eval()

    # transformations depending on the model
    # rescale, center crop, normalize, and others (ex: ToBGR, ToRange255)
    tf_img = TransformImage(model)

    """
    TODO(WG): Would be nice to use something like DataParallel, but that only does forward pass on given module.
    Need to stop before logits step. 
    Should create wrapper for pretrainedmodels that does the MPI-like ops across GPUs on model.features modules:
    1) replicated
    2) scatter
    3) parallel_apply
    4) gather
    Would have to know what layers are being used on each model. 
    """
    if torch.cuda.is_available():
        model = model.cuda(device=gpu_ids[0])

    return tf_img, model


def process_batches(batches, ftype, gpu_list, model, logger=None):
    done_batches = []
    for i, batch in enumerate(batches):
        if torch.cuda.is_available():
            batch = batch.cuda(device=gpu_list[0])

        output_features = model.features(batch)
        output_features = output_features.data.cpu()

        conv_size = output_features.shape[-1]

        if ftype == 'nasnetalarge' or ftype == 'pnasnet5large':
            relu = nn.ReLU()
            rf = relu(output_features)
            avg_pool = nn.AvgPool2d(conv_size, stride=1, padding=0)
            out_feats = avg_pool(rf)
        else:
            avg_pool = nn.AdaptiveAvgPool2d((1, 1))
            out_feats = avg_pool(output_features)

        out_feats = out_feats.view(out_feats.size(0), -1)
        if logger: logger.info('Processed {}/{} batches.\r'.format(i + 1, len(batches)))

        done_batches.append(out_feats)
    feats = np.concatenate(done_batches, axis=0)
    return feats