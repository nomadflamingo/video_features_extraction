"""
LICENSE:
The wrapping code is under MIT, yet, it utilizes `torchvision` library which is under [BSD 3-Clause "New" or "Revised" License](https://github.com/pytorch/vision/blob/master/LICENSE).
"""

import os
from typing import Dict, Union

import cv2
import numpy as np
import torch
import torchvision.models as models
import torchvision.transforms as transforms
from tqdm import tqdm
from utils.utils import (action_on_extraction, form_list_from_user_input,
                         reencode_video_with_diff_fps,
                         show_predictions_on_dataset)
from torchvision.models.efficientnet import EfficientNet_V2_S_Weights, EfficientNet_V2_L_Weights
# import traceback


RESIZE_SIZE = 256
CENTER_CROP_SIZE = 224
TRAIN_MEAN = [0.485, 0.456, 0.406]
TRAIN_STD = [0.229, 0.224, 0.225]


class ExtractEfficientNet(torch.nn.Module):

    def __init__(self, args):
        super(ExtractEfficientNet, self).__init__()
        self.feature_type = args.feature_type
        self.path_list = form_list_from_user_input(args)
        self.batch_size = args.batch_size
        self.central_crop_size = CENTER_CROP_SIZE
        self.extraction_fps = args.extraction_fps
        self.transforms = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(RESIZE_SIZE),
            transforms.CenterCrop(CENTER_CROP_SIZE),
            transforms.ToTensor(),
            transforms.Normalize(mean=TRAIN_MEAN, std=TRAIN_STD)
        ])
        self.show_pred = args.show_pred
        self.keep_tmp_files = args.keep_tmp_files
        self.on_extraction = args.on_extraction
        # not used, create an issue if you would like to save the frames
        self.tmp_path = os.path.join(args.tmp_path, self.feature_type)
        self.output_path = os.path.join(args.output_path, self.feature_type)
        self.progress = tqdm(total=len(self.path_list))

    def forward(self, indices: torch.LongTensor):
        '''
        Arguments:
            indices {torch.LongTensor} -- indices to self.path_list
        '''
        device = indices.device

        if self.feature_type == 'efficientnet_v2_s':
            model = models.efficientnet_v2_s(weights=EfficientNet_V2_S_Weights.DEFAULT)
        elif self.feature_type == 'efficientnet_v2_l':
            model = models.efficientnet_v2_l(weights=EfficientNet_V2_L_Weights.DEFAULT)
        else:
            raise NotImplementedError

        model = model.to(device)
        model.eval()
        # save the pre-trained classifier for show_preds and replace it in the net with identity
        model_class = model.classifier
        model.classifier = torch.nn.Identity()

        for idx in indices:
            # when error occurs might fail silently when run from torch data parallel
            try:
                feats_dict = self.extract(device, model, model_class, self.path_list[idx])
                action_on_extraction(feats_dict, self.path_list[idx], self.output_path, self.on_extraction)
            except KeyboardInterrupt:
                raise KeyboardInterrupt
            except Exception as e:
                # prints only the last line of an error. Use `traceback.print_exc()` for the whole traceback
                print(e)
                print(f'Extraction failed at: {self.path_list[idx]} with error (↑). Continuing extraction')

            # update tqdm progress bar
            self.progress.update()

    def extract(self, device: torch.device, model: torch.nn.Module, classifier: torch.nn.Module,
                video_path: Union[str, None] = None) -> Dict[str, np.ndarray]:
        '''The extraction call. Made to clean the forward call a bit.

        Arguments:
            device {torch.device}
            model {torch.nn.Module}
            classifier {torch.nn.Module} -- pre-trained classification layer, will be used if
                                            show_pred is True

        Keyword Arguments:
            video_path {Union[str, None]} -- if you would like to use import it and use it as
                                             "path -> model"-fashion (default: {None})

        Returns:
            Dict[str, np.ndarray]: 'features_nme', 'fps', 'timestamps_ms'
        '''
        def _run_on_a_batch(vid_feats, batch, model, classifier, device):
            batch = torch.cat(batch).to(device)

            with torch.no_grad():
                batch_feats = model(batch)
                vid_feats.extend(batch_feats.tolist())
                # show predicitons on imagenet dataset (might be useful for debugging)
                if self.show_pred:
                    logits = classifier(batch_feats)
                    show_predictions_on_dataset(logits, 'imagenet')

        # take the video, change fps and save to the tmp folder
        if self.extraction_fps is not None:
            video_path = reencode_video_with_diff_fps(video_path, self.tmp_path, self.extraction_fps)

        # read a video
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        timestamps_ms = []
        batch = []
        vid_feats = []

        # sometimes when the target fps is 1 or 2, the first frame of the reencoded video is missing
        # and cap.read returns None but the rest of the frames are ok. timestep is 0.0 for the 2nd frame in
        # this case
        first_frame = True
        while cap.isOpened():
            frame_exists, rgb = cap.read()

            if first_frame:
                first_frame = False
                if frame_exists is False:
                    continue

            if frame_exists:
                timestamps_ms.append(cap.get(cv2.CAP_PROP_POS_MSEC))
                # prepare data (first -- transform, then -- unsqueeze)
                rgb = cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB)
                rgb = self.transforms(rgb)
                rgb = rgb.unsqueeze(0)
                batch.append(rgb)
                # when batch is formed to inference
                if len(batch) == self.batch_size:
                    _run_on_a_batch(vid_feats, batch, model, classifier, device)
                    # clean up the batch list
                    batch = []
            else:
                # if the last batch was smaller than the batch size
                if len(batch) != 0:
                    _run_on_a_batch(vid_feats, batch, model, classifier, device)
                cap.release()
                break

        # removes the video with different fps if it was created to preserve disk space
        if (self.extraction_fps is not None) and (not self.keep_tmp_files):
            os.remove(video_path)

        features_with_meta = {
            self.feature_type: np.array(vid_feats),
            'fps': np.array(fps),
            'timestamps_ms': np.array(timestamps_ms)
        }

        return features_with_meta
