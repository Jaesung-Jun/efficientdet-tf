"""
Copyright 2017-2018 Fizyr (https://fizyr.com)
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import math
from typing import List, Union, Tuple, Sequence

import numpy as np
import tensorflow as tf

from . import bndbox
from . import compute_overlap as iou


class AnchorGenerator(object):
    
    def __init__(self, 
                 size: float,
                 aspect_ratios: Sequence[float],
                 stride: int = 1):
        """
        RetinaNet input examples:
            size: 32
            aspect_ratios: [0.5, 1, 2]
        """
        self.size = size
        self.stride = stride

        self.aspect_ratios = aspect_ratios
        self.anchor_scales = [
            2 ** 0,
            2 ** (1 / 3.0),
            2 ** (2 / 3.0)]

        self.anchors = self._generate()
    
    def __call__(self, *args, **kwargs):
        return self.tile_anchors_over_feature_map(*args, **kwargs)

    def tile_anchors_over_feature_map(self, feature_map_shape):
        """
        Tile anchors over all feature map positions

        Parameters
        ----------
        feature_map: Tuple[int, int, int] H, W , C
            Feature map where anchors are going to be tiled
        
        Returns
        --------
        tf.Tensor of shape [BATCH, N_BOXES, 4]
        """
        def arange(limit):
            return tf.range(0, limit, dtype=tf.float32)
        
        h = feature_map_shape[0]
        w = feature_map_shape[1]

        shift_x = (arange(w) + 0.5) * self.stride
        shift_y = (arange(h) + 0.5) * self.stride

        shift_x, shift_y = tf.meshgrid(shift_x, shift_y)
        shift_x = tf.reshape(shift_x, [-1])
        shift_y = tf.reshape(shift_y, [-1])

        shifts = tf.stack([shift_x, shift_y, shift_x, shift_y], axis=0)
        shifts = tf.transpose(shifts)

        # add A anchors (1, A, 4) to
        # cell K shifts (K, 1, 4) to get
        # shift anchors (K, A, 4)
        # reshape to (K*A, 4) shifted anchors
        A = len(self)
        K = shifts.shape[0]
    
        all_anchors = (tf.reshape(self.anchors, [1, A, 4]) 
                       + tf.cast(tf.reshape(shifts, [K, 1, 4]), tf.float64))
        all_anchors = tf.reshape(all_anchors, [K * A, 4])

        return all_anchors

    def _generate(self) -> tf.Tensor:
        num_anchors = len(self)
        ratios = np.array(self.aspect_ratios)
        scales = np.array(self.anchor_scales)

        # initialize output anchors
        anchors = np.zeros((num_anchors, 4))

        # scale base_size
        anchors[:, 2:] = self.size * np.tile(scales, (2, len(ratios))).T

        # compute areas of anchors
        areas = anchors[:, 2] * anchors[:, 3]

        # correct for ratios
        anchors[:, 2] = np.sqrt(areas / np.repeat(ratios, len(scales)))
        anchors[:, 3] = anchors[:, 2] * np.repeat(ratios, len(scales))

        # transform from (x_ctr, y_ctr, w, h) -> (x1, y1, x2, y2)
        anchors[:, 0::2] -= np.tile(anchors[:, 2] * 0.5, (2, 1)).T
        anchors[:, 1::2] -= np.tile(anchors[:, 3] * 0.5, (2, 1)).T

        return tf.constant(anchors)

    def __len__(self):
        return len(self.aspect_ratios) * len(self.anchor_scales)


def anchor_targets_bbox(anchors: tf.Tensor,
                        images: tf.Tensor,
                        bndboxes: tf.Tensor,
                        labels: tf.Tensor,
                        num_classes: int,
                        padding_value: int = -1,
                        negative_overlap: float = 0.4,
                        positive_overlap: float = 0.5) -> Tuple[tf.Tensor,
                                                                tf.Tensor]:
    """ 
    Generate anchor targets for bbox detection.

    Parameters
    ----------
    anchors: tf.Tensor 
        Annotations of shape (N, 4) for (x1, y1, x2, y2).
    images: tf.Tensor
        Array of shape [BATCH, H, W, C] containing images.
    bndboxes: tf.Tensor
        Array of shape [BATCH, N, 4] contaning ground truth boxes
    labels: tf.Tensor
        Array of shape [BATCH, N] containing the labels for each box
    num_classes: int
        Number of classes to predict.
    negative_overlap: float, default 0.4
        IoU overlap for negative anchors 
        (all anchors with overlap < negative_overlap are negative).
    positive_overlap: float, default 0.5
        IoU overlap or positive anchors 
        (all anchors with overlap > positive_overlap are positive).
    padding_value: int
        Value used to pad labels
        
    Returns
    --------
    Tuple[tf.Tensor, tf.Tensor]
        labels_batch: 
            batch that contains labels & anchor states 
            (tf.Tensor of shape (batch_size, N, num_classes + 1),
            where N is the number of anchors for an image and the last 
            column defines the anchor state (-1 for ignore, 0 for bg, 1 for fg).
        regression_batch: 
            batch that contains bounding-box regression targets for an 
            image & anchor states (tf.Tensor of shape (batch_size, N, 4 + 1),
            where N is the number of anchors for an image, the first 4 columns 
            define regression targets for (x1, y1, x2, y2) and the
            last column defines anchor states (-1 for ignore, 0 for bg, 1 for fg).
    """
    batch_size = images.shape[0]
    n_anchors = anchors.shape[0]

    result = compute_gt_annotations(anchors, 
                                    bndboxes,
                                    negative_overlap, 
                                    positive_overlap)
    positive_indices, ignore_indices, argmax_overlaps_inds = result

    # Add padded instances to ignore indices
    chose_labels = tf.gather_nd(labels, argmax_overlaps_inds)
    chose_labels = tf.reshape(chose_labels, [batch_size, -1])
    no_padding_mask = tf.not_equal(chose_labels, padding_value)
    # Remove from positive the paddings
    positive_indices = tf.logical_and(positive_indices, no_padding_mask)
    # Add padded instances to ignore instances
    ignore_indices = tf.logical_or(ignore_indices, 
                                   tf.logical_not(no_padding_mask))
    
    # Expand ignore indices with out of image anchors
    x_anchor_centre = (anchors[:, 0] + anchors[:, 2]) / 2.
    y_anchor_centre = (anchors[:, 1] + anchors[:, 3]) / 2.
    out_x = tf.greater_equal(x_anchor_centre, images.shape[2])
    out_y = tf.greater_equal(y_anchor_centre, images.shape[1])
    out_mask = tf.logical_or(out_x, out_y)
    ignore_indices = tf.logical_or(ignore_indices, out_mask)

    # Labels per anchor 
    # if is positive index add the class, else 0
    # To ignore the label add -1
    labels_per_anchor = tf.where(positive_indices, chose_labels, 0)
    labels_per_anchor = tf.where(ignore_indices, -1, labels_per_anchor)
    labels_per_anchor = tf.one_hot(labels_per_anchor, 
                                   axis=-1, depth=num_classes, off_value=0)
    labels_per_anchor = tf.cast(labels_per_anchor, tf.float32)

    # Add regression for each anchor
    chose_bndboxes = tf.gather_nd(bndboxes, argmax_overlaps_inds)
    chose_bndboxes = tf.reshape(chose_bndboxes, [batch_size, -1, 4])
    regression_per_anchor = tf.zeros([batch_size, n_anchors, 4])
    regression_per_anchor = bbox_transform(anchors, chose_bndboxes)
    
    # Generate extra label to add the state of the label. 
    # (It should be ignored?)
    indices = tf.cast(positive_indices, tf.float32)
    indices = tf.where(ignore_indices, -1, indices)
    indices = tf.expand_dims(indices, -1)

    labels_per_anchor = tf.concat([labels_per_anchor, indices], axis=-1)
    regression_per_anchor = tf.concat(
        [regression_per_anchor, indices], axis=-1)

    return regression_per_anchor, labels_per_anchor


def compute_gt_annotations(anchors: tf.Tensor,
                           annotations: tf.Tensor,
                           negative_overlap=0.4,
                           positive_overlap=0.5) -> Tuple[tf.Tensor,
                                                          tf.Tensor,
                                                          tf.Tensor]:
    """ 
    Obtain indices of gt annotations with the greatest overlap.
    
    Parameters
    ----------
    anchors: tf.Tensor
        Annotations of shape [N, 4] for (x1, y1, x2, y2).
    annotations: tf.Tensor 
        shape [BATCH, N, 4] for (x1, y1, x2, y2).
    negative_overlap: float, default 0.4
        IoU overlap for negative anchors 
        (all anchors with overlap < negative_overlap are negative).
    positive_overlap: float, default 0.5
        IoU overlap or positive anchors 
        (all anchors with overlap > positive_overlap are positive).

    Returns
    -------
    Tuple[tf.Tensor, tf.Tensor, np.ndarray]
        positive_indices: indices of positive anchors
        ignore_indices: indices of ignored anchors
        argmax_overlaps_inds: ordered overlaps indices
    """
    batch_size = annotations.shape[0]
    
    # Cast and reshape inputs to expected values
    anchors = tf.expand_dims(anchors, 0)
    anchors = tf.cast(anchors, tf.float64)
    anchors = tf.tile(anchors, [batch_size, 1, 1])
    annotations = tf.cast(annotations, tf.float64)

    # Compute the ious between boxes, and get the argmax indices and max values
    overlaps = bndbox.bbox_overlap(anchors, annotations)
    argmax_overlaps_inds = tf.argmax(overlaps, axis=-1, output_type=tf.int32)
    max_overlaps = tf.reduce_max(overlaps, axis=-1)

    # Generate index like [batch_idx, max_overlap]
    batched_indices = tf.ones([batch_size, anchors.shape[1]], dtype=tf.int32) 
    batched_indices = tf.multiply(tf.expand_dims(tf.range(batch_size), -1), 
                                  batched_indices)
    batched_indices = tf.reshape(batched_indices, [-1, 1])
    argmax_inds = tf.reshape(argmax_overlaps_inds, [-1, 1])
    batched_indices = tf.concat([batched_indices, argmax_inds], -1)
    max_overlap_boxes = tf.gather_nd(anchors, batched_indices)
    max_overlap_boxes = tf.reshape(max_overlap_boxes, [batch_size, -1, 4])

    # Compute areas of boxes so we can ignore 'ridiculous' sized boxes
    widths = (max_overlap_boxes[..., 2] - max_overlap_boxes[..., 0])
    heights = (max_overlap_boxes[..., 3] - max_overlap_boxes[..., 1])
    areas = widths * heights
    small_areas = tf.less(areas, 1.)
    large_areas = tf.logical_not(small_areas)

    # Assign positive indices. 
    positive_indices = tf.greater_equal(max_overlaps, positive_overlap) 
    positive_indices = tf.logical_and(positive_indices, large_areas)
    
    # Assign ignored boxes
    ignore_indices = tf.greater(max_overlaps, negative_overlap)
    ignore_indices = tf.logical_and(ignore_indices, 
                                    tf.logical_not(positive_indices))
    ignore_indices = tf.logical_or(ignore_indices, small_areas)

    return positive_indices, ignore_indices, batched_indices


def bbox_transform(anchors: tf.Tensor, gt_boxes: tf.Tensor) -> tf.Tensor:
    """Compute bounding-box regression targets for an image."""

    mean = tf.constant([0., 0., 0., 0.])
    std = np.array([0.2, 0.2, 0.2, 0.2])

    anchors = tf.cast(anchors, tf.float32)
    gt_boxes = tf.cast(gt_boxes, tf.float32)

    anchor_widths  = anchors[..., 2] - anchors[..., 0]
    anchor_heights = anchors[..., 3] - anchors[..., 1]

    targets_dx1 = (gt_boxes[..., 0] - anchors[..., 0]) / anchor_widths
    targets_dy1 = (gt_boxes[..., 1] - anchors[..., 1]) / anchor_heights
    targets_dx2 = (gt_boxes[..., 2] - anchors[..., 2]) / anchor_widths
    targets_dy2 = (gt_boxes[..., 3] - anchors[..., 3]) / anchor_heights

    targets = tf.stack(
        [targets_dx1, targets_dy1, targets_dx2, targets_dy2], axis=-1)

    targets = (targets - mean) / std

    return targets
