# coding=utf-8
# Copyright 2022 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Feature extractor class for MaskFormer."""

from typing import Dict, List, Optional, Tuple, Union, TypedDict

import numpy as np
from PIL import Image
from transformers.models.maskformer.modeling_maskformer import MaskFormerOutput

from ...feature_extraction_utils import BatchFeature, FeatureExtractionMixin
from ...file_utils import TensorType, is_torch_available
from ...image_utils import ImageFeatureExtractionMixin, is_torch_tensor
from ...utils import logging
from .configuration_maskformer import ClassSpec

if is_torch_available():
    import torch
    from torch import nn
    from torch import Tensor

logger = logging.get_logger(__name__)


ImageInput = Union[Image.Image, np.ndarray, "torch.Tensor", List[Image.Image], List[np.ndarray], List["torch.Tensor"]]


class PanopticSegmentationSegment(TypedDict):
    id: int
    category_id: int
    is_thing: bool
    label: str


class MaskFormerFeatureExtractor(FeatureExtractionMixin, ImageFeatureExtractionMixin):
    r"""
    Constructs a MaskFormer feature extractor.

    This feature extractor inherits from [`FeatureExtractionMixin`] which contains most of the main methods. Users
    should refer to this superclass for more information regarding those methods.


    Args:
        format (`str`, *optional*, defaults to `"coco_detection"`):
            Data format of the annotations. One of "coco_detection" or "coco_panoptic".
        do_resize (`bool`, *optional*, defaults to `True`):
            Whether to resize the input to a certain `size`.
        size (`int`, *optional*, defaults to 800):
            Resize the input to the given size. Only has an effect if `do_resize` is set to `True`. If size is a
            sequence like `(width, height)`, output size will be matched to this. If size is an int, smaller edge of
            the image will be matched to this number. i.e, if `height > width`, then image will be rescaled to `(size *
            height / width, size)`.
        max_size (`int`, *optional*, defaults to `1333`):
            The largest size an image dimension can have (otherwise it's capped). Only has an effect if `do_resize` is
            set to `True`.
        do_normalize (`bool`, *optional*, defaults to `True`):
            Whether or not to normalize the input with mean and standard deviation.
        image_mean (`int`, *optional*, defaults to `[0.485, 0.456, 0.406]`):
            The sequence of means for each channel, to be used when normalizing images. Defaults to the ImageNet mean.
        image_std (`int`, *optional*, defaults to `[0.229, 0.224, 0.225]`):
            The sequence of standard deviations for each channel, to be used when normalizing images. Defaults to the
            ImageNet std.
    """

    model_input_names = ["pixel_values", "pixel_mask"]

    def __init__(
        self, do_resize=True, size=800, max_size=1333, do_normalize=True, image_mean=None, image_std=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.do_resize = do_resize
        self.size = size
        self.max_size = max_size
        self.do_normalize = do_normalize
        self.image_mean = image_mean if image_mean is not None else [0.485, 0.456, 0.406]  # ImageNet mean
        self.image_std = image_std if image_std is not None else [0.229, 0.224, 0.225]  # ImageNet std

    def _resize(self, image, size, target=None, max_size=None):
        """
        Resize the image to the given size. Size can be min_size (scalar) or (width, height) tuple. If size is an int,
        smaller edge of the image will be matched to this number.

        If given, also resize the target accordingly.
        """
        if not isinstance(image, Image.Image):
            image = self.to_pil_image(image)

        def get_size_with_aspect_ratio(image_size, size, max_size=None):
            width, height = image_size
            if max_size is not None:
                min_original_size = float(min((width, height)))
                max_original_size = float(max((width, height)))
                if max_original_size / min_original_size * size > max_size:
                    size = int(round(max_size * min_original_size / max_original_size))

            if (width <= height and width == size) or (height <= width and height == size):
                return (height, width)

            if width < height:
                ow = size
                oh = int(size * height / width)
            else:
                oh = size
                ow = int(size * width / height)

            return (oh, ow)

        def get_size(image_size, size, max_size=None):
            if isinstance(size, (list, tuple)):
                return size
            else:
                # size returned must be (width, height) since we use PIL to resize images
                # so we revert the tuple
                return get_size_with_aspect_ratio(image_size, size, max_size)[::-1]

        size = get_size(image.size, size, max_size)
        rescaled_image = self.resize(image, size=size)
        # pil image have inverted width, height
        width, height = size

        has_target = target is not None

        if has_target:
            target = target.copy()
            # store original_size
            target["original_size"] = image.size
            if "masks" in target:
                #           use PyTorch as current workaround
                # TODO replace by self.resize
                masks = torch.from_numpy(target["masks"][:, None]).float()
                interpolated_masks = nn.functional.interpolate(masks, size=(height, width), mode="nearest")[:, 0] > 0.5
                target["masks"] = interpolated_masks.numpy()

        return rescaled_image, target

    def _normalize(self, image, mean, std, target=None):
        """
        Normalize the image with a certain mean and std.

        If given, also normalize the target bounding boxes based on the size of the image.
        """

        image = self.normalize(image, mean=mean, std=std)

        return image, target

    def __call__(
        self,
        images: ImageInput,
        annotations: Union[List[Dict], List[List[Dict]]] = None,
        pad_and_return_pixel_mask: Optional[bool] = True,
        return_tensors: Optional[Union[str, TensorType]] = None,
        **kwargs,
    ) -> BatchFeature:
        """
        Main method to prepare for the model one or several image(s) and optional annotations. Images are by default
        padded up to the largest image in a batch, and a pixel mask is created that indicates which pixels are
        real/which are padding.

        <Tip warning={true}>

        NumPy arrays and PyTorch tensors are converted to PIL images when resizing, so the most efficient is to pass
        PIL images.

        </Tip>

        Args:
            images (`PIL.Image.Image`, `np.ndarray`, `torch.Tensor`, `List[PIL.Image.Image]`, `List[np.ndarray]`, `List[torch.Tensor]`):
                The image or batch of images to be prepared. Each image can be a PIL image, NumPy array or PyTorch
                tensor. In case of a NumPy array/PyTorch tensor, each image should be of shape (C, H, W), where C is a
                number of channels, H and W are image height and width.

            annotations (`Dict`, `List[Dict]`, *optional*):
                The corresponding annotations in the following format: { "masks" : the target mask, with shape [C,H,W],
                "labels" : the target labels, with shape [C]}

            pad_and_return_pixel_mask (`bool`, *optional*, defaults to `True`):
                Whether or not to pad images up to the largest image in a batch and create a pixel mask.

                If left to the default, will return a pixel mask that is:

                - 1 for pixels that are real (i.e. **not masked**),
                - 0 for pixels that are padding (i.e. **masked**).

            return_tensors (`str` or [`~file_utils.TensorType`], *optional*):
                If set, will return tensors instead of NumPy arrays. If set to `'pt'`, return PyTorch `torch.Tensor`
                objects.

        Returns:
            [`BatchFeature`]: A [`BatchFeature`] with the following fields:

            - **pixel_values** -- Pixel values to be fed to a model.
            - **pixel_mask** -- Pixel mask to be fed to a model (when `pad_and_return_pixel_mask=True` or if
              *"pixel_mask"* is in `self.model_input_names`).
            - **labels** -- Optional labels to be fed to a model (when `annotations` are provided)
        """
        # Input type checking for clearer error

        valid_images = False
        valid_annotations = False

        # Check that images has a valid type
        if isinstance(images, (Image.Image, np.ndarray)) or is_torch_tensor(images):
            valid_images = True
        elif isinstance(images, (list, tuple)):
            if len(images) == 0 or isinstance(images[0], (Image.Image, np.ndarray)) or is_torch_tensor(images[0]):
                valid_images = True

        if not valid_images:
            raise ValueError(
                "Images must of type `PIL.Image.Image`, `np.ndarray` or `torch.Tensor` (single example), "
                "`List[PIL.Image.Image]`, `List[np.ndarray]` or `List[torch.Tensor]` (batch of examples)."
            )

        is_batched = bool(
            isinstance(images, (list, tuple))
            and (isinstance(images[0], (Image.Image, np.ndarray)) or is_torch_tensor(images[0]))
        )

        if not is_batched:
            images = [images]
            if annotations is not None:
                annotations = [annotations]

        # Check that annotations has a valid type
        if annotations is not None:
            # TODO ask best way to check!
            valid_annotations = type(annotations) is list and "masks" in annotations[0] and "labels" in annotations[0]
            if not valid_annotations:
                raise ValueError(
                    """
                    Annotations must of type `Dict` (single image) or `List[Dict]` (batch of images). In case of object
                    detection, each dictionary should contain the keys 'image_id' and 'annotations', with the latter
                    being a list of annotations in COCO format. In case of panoptic segmentation, each dictionary
                    should contain the keys 'file_name', 'image_id' and 'segments_info', with the latter being a list
                    of annotations in COCO format.
                    """
                )

        # transformations (resizing + normalization)
        if self.do_resize and self.size is not None:
            if annotations is not None:
                for idx, (image, target) in enumerate(zip(images, annotations)):
                    image, target = self._resize(image=image, target=target, size=self.size, max_size=self.max_size)
                    images[idx] = image
                    annotations[idx] = target
            else:
                for idx, image in enumerate(images):
                    images[idx] = self._resize(image=image, target=None, size=self.size, max_size=self.max_size)[0]

        if self.do_normalize:
            if annotations is not None:
                for idx, (image, target) in enumerate(zip(images, annotations)):
                    image, target = self._normalize(
                        image=image, mean=self.image_mean, std=self.image_std, target=target
                    )
                    images[idx] = image
                    annotations[idx] = target
            else:
                images = [
                    self._normalize(image=image, mean=self.image_mean, std=self.image_std)[0] for image in images
                ]
        # NOTE I will be always forced to pad them them since they have to be stacked in the batch dim
        encoded_inputs = self.encode_inputs(
            images, annotations, should_pad=pad_and_return_pixel_mask, return_tensors=return_tensors
        )

        if annotations is not None:
            # Convert to TensorType
            tensor_type = return_tensors
            if not isinstance(tensor_type, TensorType):
                tensor_type = TensorType(tensor_type)

            if not tensor_type == TensorType.PYTORCH:
                raise ValueError("Only PyTorch is supported for the moment.")
            else:
                if not is_torch_available():
                    raise ImportError("Unable to convert output to PyTorch tensors format, PyTorch is not installed.")

        return encoded_inputs

    def _max_by_axis(self, the_list: List[List[int]]) -> List[int]:
        maxes = the_list[0]
        for sublist in the_list[1:]:
            for index, item in enumerate(sublist):
                maxes[index] = max(maxes[index], item)
        return maxes

    def encode_inputs(
        self,
        pixel_values_list: List["torch.Tensor"],
        annotations: Optional[List[Dict]] = None,
        pad_and_return_pixel_mask: Optional[bool] = True,
        return_tensors: Optional[Union[str, TensorType]] = None,
    ):
        """
        Pad images up to the largest image in a batch and create a corresponding `pixel_mask`.

        Args:
            pixel_values_list (`List[torch.Tensor]`):
                List of images (pixel values) to be padded. Each image should be a tensor of shape (C, H, W).
            return_tensors (`str` or [`~file_utils.TensorType`], *optional*):
                If set, will return tensors instead of NumPy arrays. If set to `'pt'`, return PyTorch `torch.Tensor`
                objects.

        Returns:
            [`BatchFeature`]: A [`BatchFeature`] with the following fields:

            - **pixel_values** -- Pixel values to be fed to a model.
            - **pixel_mask** -- Pixel mask to be fed to a model (when `pad_and_return_pixel_mask=True` or if
              *"pixel_mask"* is in `self.model_input_names`).
            - **mask_labels** -- Optional mask labels of shape `(batch_size, num_classes, height, width) to be fed to a model (when `annotations` are provided)
            - **class_labels** -- Optional class labels of shape `(batch_size, num_classes) to be fed to a model (when `annotations` are provided)
        """

        max_size = self._max_by_axis([list(image.shape) for image in pixel_values_list])
        c, height, width = max_size
        pixel_values = []
        pixel_mask = []
        mask_labels = []
        class_labels = []

        for idx, image in enumerate(pixel_values_list):
            # create padded image
            if pad_and_return_pixel_mask:
                padded_image = np.zeros((c, height, width), dtype=np.float32)
                padded_image[: image.shape[0], : image.shape[1], : image.shape[2]] = np.copy(image)
                image = padded_image
            pixel_values.append(image)
            # if we have a target, pad it
            if annotations:
                annotation = annotations[idx]
                masks = annotation["masks"]
                if pad_and_return_pixel_mask:
                    padded_masks = np.zeros((masks.shape[0], height, width), dtype=masks.dtype)
                    padded_masks[:, : padded_masks.shape[1], : padded_masks.shape[2]] = np.copy(padded_masks)
                    masks = padded_masks
                mask_labels.append(masks)
                class_labels.append(annotation["labels"])
            if pad_and_return_pixel_mask:
                # create pixel mask
                mask = np.zeros((height, width), dtype=np.int64)
                mask[: image.shape[1], : image.shape[2]] = True
                pixel_mask.append(mask)

        # return as BatchFeature
        data = {
            "pixel_values": pixel_values,
            "pixel_mask": pixel_mask,
            "mask_labels": mask_labels,
            "class_labels": class_labels,
        }

        encoded_inputs = BatchFeature(data=data, tensor_type=return_tensors)
        if annotations:
            # BatchFeature doesn't support nested dicts, adding after it
            tensor_type = return_tensors
            if not isinstance(tensor_type, TensorType):
                tensor_type = TensorType(tensor_type)
            if not tensor_type == TensorType.PYTORCH:
                raise ValueError("Only PyTorch is supported for the moment.")
            else:
                if not is_torch_available():
                    raise ImportError("Unable to convert output to PyTorch tensors format, PyTorch is not installed.")

        return encoded_inputs

    def post_process_segmentation(self, outputs: MaskFormerOutput) -> Tensor:
        """Converts the output of [`MaskFormerModel`] into image segmentation predictions. Only supports PyTorch.

        Args:
            outputs (MaskFormerOutput): The outputs from MaskFor

        Returns:
            Tensor: A tensor of shape `batch_size, num_labels, height, width`
        """
        # class_queries_logitss has shape [BATCH, QUERIES, CLASSES + 1]
        class_queries_logits = outputs.class_queries_logits
        # masks_queries_logits has shape [BATCH, QUERIES, HEIGHT, WIDTH]
        masks_queries_logits = outputs.masks_queries_logits
        # remove the null class `[..., :-1]`
        masks_classes: Tensor = class_queries_logits.softmax(dim=-1)[..., :-1]
        # mask probs has shape [BATCH, QUERIES, HEIGHT, WIDTH]
        masks_probs: Tensor = masks_queries_logits.sigmoid()
        # now we want to sum over the queries,
        # $ out_{c,h,w} =  \sum_q p_{q,c} * m_{q,h,w} $
        # where $ softmax(p) \in R^{q, c} $ is the mask classes
        # and $ sigmoid(m) \in R^{q, h, w}$ is the mask probabilities
        # b(atch)q(uery)c(lasses), b(atch)q(uery)h(eight)w(idth)
        segmentation: Tensor = torch.einsum("bqc, bqhw -> bchw", masks_classes, masks_probs)

        return segmentation

    def remove_low_and_no_objects(
        self, masks: Tensor, scores: Tensor, labels: Tensor, object_mask_threshold: float, num_labels: int
    ) -> Tuple[Tensor, Tensor, Tensor]:
        """

        Binarize the given masks using `object_mask_threshold`, it returns the associated values of `masks`, `scores` and `labels`

        Args:
            masks (Tensor): A tensor of shape `(num_queries, height, width)`
            scores (Tensor): A tensor of shape `(num_queries)`
            labels (Tensor): A tensor of shape `(num_queries)`
            object_mask_threshold (float): A number between 0 and 1 used to binarize the masks

        Raises:
            ValueError: When the first dimension doesn't match in all input tensors

        Returns:
            Tuple[Tensor, Tensor, Tensor]: The inputs tensors without the region with `< object_mask_threshold`
        """
        if not (masks.shape[0] == scores.shape[0] == labels.shape[0]):
            raise ValueError("mask, scores and labels must have the same shape!")

        to_keep: Tensor = labels.ne(num_labels) & (scores > object_mask_threshold)

        return masks[to_keep], scores[to_keep], labels[to_keep]

    def post_process_panoptic_segmentation(
        self,
        outputs: MaskFormerOutput,
        object_mask_threshold: Optional[float] = 0.8,
        overlap_mask_area_threshold: Optional[float] = 0.8,
    ) -> Tensor:
        """
        Converts the output of [`MaskFormerModel`] into image panoptic segmentation predictions. Only supports PyTorch.

        Args:
            outputs (MaskFormerOutput): [description]
            object_mask_threshold (Optional[float], optional): [description]. Defaults to 0.8.
            overlap_mask_area_threshold (Optional[float], optional): [description]. Defaults to 0.8.

        Returns:
            Tensor: [description]
        """
        # class_queries_logitss has shape [BATCH, QUERIES, CLASSES + 1]
        class_queries_logits: Tensor = outputs.class_queries_logits
        # keep track of the number of labels, subtract -1 for null class
        num_labels: int = class_queries_logits.shape[-1] - 1
        # masks_queries_logits has shape [BATCH, QUERIES, HEIGHT, WIDTH]
        masks_queries_logits: Tensor = outputs.masks_queries_logits
        # since all images are padded, they all have the same spatial dimensions
        _, _, height, width = masks_queries_logits.shape
        # for each query, the best scores and their indeces
        pred_scores, pred_labels = nn.functional.softmax(class_queries_logits, dim=-1).max(-1)
        # pred_scores and pred_labels shape = [BATH,NUM_QUERIES]
        mask_probs = masks_queries_logits.sigmoid()
        # mask probs has shape [BATCH, QUERIES, HEIGHT, WIDTH]
        # now, we need to iterate over the batch size to correctly process the segmentation we got from the queries using our thresholds. Even if the original predicted masks have the same shape across the batch, they won't after thresholding so batch-wise operations are impossible

        results: List[Dict[str, Tensor]] = []
        for (mask_probs, pred_scores, pred_labels) in zip(mask_probs, pred_scores, pred_labels):

            mask_probs, pred_scores, pred_labels = self.remove_low_and_no_objects(
                mask_probs, pred_scores, pred_labels, object_mask_threshold, num_labels
            )
            we_detect_something: bool = mask_probs.shape[0] > 0

            segmentation: Tensor = torch.zeros((height, width), dtype=torch.int32, device=mask_probs.device)
            segments: List[PanopticSegmentationSegment] = []

            if we_detect_something:
                current_segment_id: int = 0
                # weight each mask by its score
                mask_probs *= pred_scores.view(-1, 1, 1)
                # find out for each pixel what is the most likely class to be there
                mask_labels: Tensor = mask_probs.argmax(0)
                # mask_labels shape = [H,W] where each pixel has a class label
                stuff_memory_list: Dict[str, int] = {}
                # this is a map between stuff and segments id, the used it to keep track of the instances of one class
                for k in range(pred_labels.shape[0]):
                    pred_class: int = pred_labels[k].item()
                    # check if pred_class is not a "thing", so it can be merged with other instance. For example, class "sky" cannot have more then one instance
                    class_spec: ClassSpec = self.model.config.dataset_metadata["classes"][pred_class]
                    is_stuff = not class_spec["is_thing"]
                    # get the mask associated with the k class
                    mask_k: Tensor = mask_labels == k
                    # create the area, since bool we just need to sum :)
                    mask_k_area: Tensor = mask_k.sum()
                    # this is the area of all the stuff in query k
                    # TODO not 100%, why are the taking the k query here????
                    original_area: Tensor = (mask_probs[k] >= 0.5).sum()

                    mask_does_exist: bool = mask_k_area > 0 and original_area > 0

                    if mask_does_exist:
                        # find out how much of the all area mask_k is using
                        area_ratio: float = mask_k_area / original_area
                        mask_k_is_overlapping_enough: bool = area_ratio.item() > overlap_mask_area_threshold

                        if mask_k_is_overlapping_enough:
                            # merge stuff regions
                            if pred_class in stuff_memory_list:
                                current_segment_id = stuff_memory_list[pred_class]
                            else:
                                current_segment_id += 1
                            # then we update out mask with the current segment
                            segmentation[mask_k] = current_segment_id
                            segments.append(
                                {
                                    "id": current_segment_id,
                                    "category_id": pred_class,
                                    "is_thing": not is_stuff,
                                    "label": class_spec["label"],
                                }
                            )
                            if is_stuff:
                                stuff_memory_list[pred_class] = current_segment_id

                    results.append({"segmentation": segmentation, "segments": segments})

        return results