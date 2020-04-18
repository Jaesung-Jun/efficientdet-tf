import unittest

import unittest

import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt

import efficientdet.utils as utils
import efficientdet.data.voc as voc
import efficientdet.config as config
from efficientdet.data.preprocess import unnormalize_image


class VOCDatasetTest(unittest.TestCase):

    def generate_anchors(self,
                         anchors_config: config.AnchorsConfig,
                         im_shape: int) -> tf.Tensor:

        anchors_gen = [utils.anchors.AnchorGenerator(
                size=anchors_config.sizes[i - 3],
                aspect_ratios=anchors_config.ratios,
                stride=anchors_config.strides[i - 3]) 
                for i in range(3, 8)]
        
        shapes = [im_shape // (2 ** (x - 2)) for x in range(3, 8)]

        anchors = [g((size, size, 3))
                for g, size in zip(anchors_gen, shapes)]

        return tf.concat(anchors, axis=0)

    def test_compute_gt(self):
        ds = voc.build_dataset(im_input_size=(512, 512))

        anchors = self.generate_anchors(config.AnchorsConfig(), 512)
        im, (l, bbs) = next(iter(ds.take(1)))
        im = unnormalize_image(im)
        
        gt_reg, gt_labels = utils.anchors.anchor_targets_bbox(
            anchors, 
            tf.expand_dims(im, 0), 
            tf.expand_dims(bbs, 0), 
            tf.expand_dims(l, 0), 
            tf.constant(20))
        

       
        near_mask = gt_reg[0, :, -1] == 1
        nearest_regressors = tf.expand_dims(
            tf.boolean_mask(gt_reg[0], near_mask)[:, :-1], 0)
        nearest_anchors = tf.expand_dims(anchors[near_mask], 0)

        # apply regression to boxes
        regressed_boxes = utils.bndbox.regress_bndboxes(
            nearest_anchors, nearest_regressors)

        im = utils.visualizer.draw_boxes(
            im, regressed_boxes[0], colors=[(255, 0, 0)])
            
        plt.imshow(im)
        plt.axis('off')
        plt.show(block=True)

        print('GT shapes:', gt_labels.shape, gt_reg.shape)
        print('Found any overlapping anchor?', 
                np.any(gt_labels[:, :, -1] == 1.))


if __name__ == "__main__":
    unittest.main()