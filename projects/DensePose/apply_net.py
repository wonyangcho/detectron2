#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.

import argparse
import glob
import logging
import os
import pickle
import sys
from typing import Any, ClassVar, Dict, List
from PIL import Image
import numpy as np

import torch

from detectron2.config import CfgNode, get_cfg
from detectron2.data.detection_utils import read_image
from detectron2.engine.defaults import DefaultPredictor
from detectron2.structures.instances import Instances
from detectron2.utils.logger import setup_logger

from densepose import add_densepose_config, add_hrnet_config
from densepose.utils.logger import verbosity_to_level
from densepose.vis.base import CompoundVisualizer
from densepose.vis.bounding_box import ScoredBoundingBoxVisualizer
from densepose.vis.densepose_outputs_vertex import DensePoseOutputsVertexVisualizer
from densepose.vis.densepose_results import (
    DensePoseResultsContourVisualizer,
    DensePoseResultsFineSegmentationVisualizer,
    DensePoseResultsUVisualizer,
    DensePoseResultsVVisualizer,
)
from densepose.vis.densepose_results_textures import (
    DensePoseResultsVisualizerWithTexture,
    get_texture_atlas,
)
from densepose.vis.extractor import CompoundExtractor, DensePoseResultExtractor, create_extractor

DOC = """Apply Net - a tool to print / visualize DensePose results
"""

LOGGER_NAME = "apply_net"
logger = logging.getLogger(LOGGER_NAME)

_ACTION_REGISTRY: Dict[str, "Action"] = {}


class Action(object):
    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        parser.add_argument(
            "-v",
            "--verbosity",
            action="count",
            help="Verbose mode. Multiple -v options increase the verbosity.",
        )


def register_action(cls: type):
    """
    Decorator for action classes to automate action registration
    """
    global _ACTION_REGISTRY
    _ACTION_REGISTRY[cls.COMMAND] = cls
    return cls


class InferenceAction(Action):
    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(InferenceAction, cls).add_arguments(parser)
        parser.add_argument("cfg", metavar="<config>", help="Config file")
        parser.add_argument("model", metavar="<model>", help="Model file")
        parser.add_argument("input", metavar="<input>", help="Input data")
        parser.add_argument(
            "--opts",
            help="Modify config options using the command-line 'KEY VALUE' pairs",
            default=[],
            nargs=argparse.REMAINDER,
        )

    @classmethod
    def execute(cls: type, args: argparse.Namespace):
        logger.info(f"Loading config from {args.cfg}")
        opts = []
        cfg = cls.setup_config(args.cfg, args.model, args, opts)
        logger.info(f"Loading model from {args.model}")
        predictor = DefaultPredictor(cfg)
        logger.info(f"Loading data from {args.input}")
        file_list = cls._get_input_file_list(args.input)
        if len(file_list) == 0:
            logger.warning(f"No input images for {args.input}")
            return
        context = cls.create_context(args, cfg)
        for file_name in file_list:
            img = read_image(file_name, format="BGR")  # predictor expects BGR image.
            with torch.no_grad():
                outputs = predictor(img)["instances"]
                cls.execute_on_outputs(context, {"file_name": file_name, "image": img}, outputs)
        cls.postexecute(context)

    @classmethod
    def setup_config(
        cls: type, config_fpath: str, model_fpath: str, args: argparse.Namespace, opts: List[str]
    ):
        cfg = get_cfg()
        add_densepose_config(cfg)
        add_hrnet_config(cfg)
        cfg.merge_from_file(config_fpath)
        cfg.merge_from_list(args.opts)
        if opts:
            cfg.merge_from_list(opts)
        cfg.MODEL.WEIGHTS = model_fpath
        cfg.freeze()
        return cfg

    @classmethod
    def _get_input_file_list(cls: type, input_spec: str):
        if os.path.isdir(input_spec):
            file_list = [
                os.path.join(input_spec, fname)
                for fname in os.listdir(input_spec)
                if os.path.isfile(os.path.join(input_spec, fname))
            ]
        elif os.path.isfile(input_spec):
            file_list = [input_spec]
        else:
            file_list = glob.glob(input_spec)
        return file_list


@register_action
class DumpAction(InferenceAction):
    """
    Dump action that outputs results to a pickle file
    """

    COMMAND: ClassVar[str] = "dump"

    @classmethod
    def add_parser(cls: type, subparsers: argparse._SubParsersAction):
        parser = subparsers.add_parser(cls.COMMAND, help="Dump model outputs to a file.")
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.execute)

    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(DumpAction, cls).add_arguments(parser)
        parser.add_argument(
            "--output",
            metavar="<dump_file>",
            default="results.pkl",
            help="File name to save dump to",
        )

    @classmethod
    def execute_on_outputs(
        cls: type, context: Dict[str, Any], entry: Dict[str, Any], outputs: Instances
    ):
        image_fpath = entry["file_name"]

        temp_w = 0
        temp_h = 0
        with Image.open(image_fpath) as temp_image:
           temp_w,temp_h = temp_image.size
        
        logger.info(f"Processing {image_fpath}")
        result = {"file_name": image_fpath}
        if outputs.has("scores"):
            result["scores"] = outputs.get("scores").cpu()
        if outputs.has("pred_boxes"):
            result["pred_boxes_XYXY"] = outputs.get("pred_boxes").tensor.cpu()
            if outputs.has("pred_densepose"):
                result["pred_densepose"], _ = DensePoseResultExtractor()(outputs)
        context["results"].append(result)


        ########################
        
        out_fname = context["out_fname"]
        out_dir = os.path.dirname(out_fname)
        
        if len(out_dir) > 0 and not os.path.exists(out_dir):
            os.makedirs(out_dir)

        out_ext = os.path.splitext(out_fname)[1]

        out_fname = os.path.basename(image_fpath)
        out_fname = os.path.join(out_dir,out_fname)

        out_npy_fname = os.path.splitext(out_fname)[0]+".npy"
        out_fname = os.path.splitext(out_fname)[0]+out_ext
        
        with open(out_fname, "wb") as hFile:
            pickle.dump(context["results"], hFile)
            logger.info(f"Output saved to {out_fname}")

        
        ########################

        bbox_xyxy = result['pred_boxes_XYXY'][0]
        x1, y1, x2, y2 = int(bbox_xyxy[0]), int(bbox_xyxy[1]), int(bbox_xyxy[2]), int(bbox_xyxy[3]) 
        w = int(bbox_xyxy[2]-bbox_xyxy[0])
        h = int(bbox_xyxy[3]-bbox_xyxy[1])
        img_final_arr =  np.zeros((temp_h,temp_w,3))   

        pred_densepose = result['pred_densepose']
        
        logger.info(f"bbox_xyxy  {bbox_xyxy}")
        logger.info(f"pred_densepose[0].labels shape {pred_densepose[0].labels.shape}")
        logger.info(f"pred_densepose[0].labelsunsqueeze(0) shape {pred_densepose[0].labels.unsqueeze(0).shape}")
        logger.info(f"pred_densepose[0].uv shape {pred_densepose[0].uv.shape}")
        
        iuv_arr = torch.cat([pred_densepose[0].labels.unsqueeze(0), pred_densepose[0].uv],0).cpu().numpy()
        
        mask = np.transpose(iuv_arr,(1,2,0))
        

        
        img_final_arr[y1:y1+h,x1:x1+w,:] = mask
        dense_img = img_final_arr
        
        dense_img = Image.fromarray(dense_img.astype(np.uint8))
        dense_img = dense_img.resize((256, 512),Image.BICUBIC)
        dense_img = np.array(dense_img)

        logger.info(f"img width  {temp_w}, {temp_h}")
        logger.info(f"dense_img shape {dense_img.shape}")
        logger.info(f"iuv_arr shape {iuv_arr.shape}")
        logger.info(f"dense_img shape {dense_img.shape}")


        np.save(out_npy_fname,dense_img)
        logger.info(f"Output saved to {out_npy_fname} {dense_img.shape}")
        ########################

        context["results"].clear()

    @classmethod
    def create_context(cls: type, args: argparse.Namespace, cfg: CfgNode):
        context = {"results": [], "out_fname": args.output}
        return context

    @classmethod
    def postexecute(cls: type, context: Dict[str, Any]):
        out_fname = context["out_fname"]
        out_dir = os.path.dirname(out_fname)
        if len(out_dir) > 0 and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        with open(out_fname, "wb") as hFile:
            pickle.dump(context["results"], hFile)
            logger.info(f"Output saved to {out_fname}")


@register_action
class ShowAction(InferenceAction):
    """
    Show action that visualizes selected entries on an image
    """

    COMMAND: ClassVar[str] = "show"
    VISUALIZERS: ClassVar[Dict[str, object]] = {
        "dp_contour": DensePoseResultsContourVisualizer,
        "dp_segm": DensePoseResultsFineSegmentationVisualizer,
        "dp_u": DensePoseResultsUVisualizer,
        "dp_v": DensePoseResultsVVisualizer,
        "dp_iuv_texture": DensePoseResultsVisualizerWithTexture,
        "dp_vertex": DensePoseOutputsVertexVisualizer,
        "bbox": ScoredBoundingBoxVisualizer,
    }

    @classmethod
    def add_parser(cls: type, subparsers: argparse._SubParsersAction):
        parser = subparsers.add_parser(cls.COMMAND, help="Visualize selected entries")
        cls.add_arguments(parser)
        parser.set_defaults(func=cls.execute)

    @classmethod
    def add_arguments(cls: type, parser: argparse.ArgumentParser):
        super(ShowAction, cls).add_arguments(parser)
        parser.add_argument(
            "visualizations",
            metavar="<visualizations>",
            help="Comma separated list of visualizations, possible values: "
            "[{}]".format(",".join(sorted(cls.VISUALIZERS.keys()))),
        )
        parser.add_argument(
            "--min_score",
            metavar="<score>",
            default=0.8,
            type=float,
            help="Minimum detection score to visualize",
        )
        parser.add_argument(
            "--nms_thresh", metavar="<threshold>", default=None, type=float, help="NMS threshold"
        )
        parser.add_argument(
            "--texture_atlas", metavar="<texture_atlas>", default=None, help="Texture atlas file"
        )
        parser.add_argument(
            "--output",
            metavar="<image_file>",
            default="outputres.png",
            help="File name to save output to",
        )

    @classmethod
    def setup_config(
        cls: type, config_fpath: str, model_fpath: str, args: argparse.Namespace, opts: List[str]
    ):
        opts.append("MODEL.ROI_HEADS.SCORE_THRESH_TEST")
        opts.append(str(args.min_score))
        if args.nms_thresh is not None:
            opts.append("MODEL.ROI_HEADS.NMS_THRESH_TEST")
            opts.append(str(args.nms_thresh))
        cfg = super(ShowAction, cls).setup_config(config_fpath, model_fpath, args, opts)
        return cfg

    @classmethod
    def execute_on_outputs(
        cls: type, context: Dict[str, Any], entry: Dict[str, Any], outputs: Instances
    ):
        import cv2
        import numpy as np

        visualizer = context["visualizer"]
        extractor = context["extractor"]
        image_fpath = entry["file_name"]
        logger.info(f"Processing {image_fpath}")
        image = cv2.cvtColor(entry["image"], cv2.COLOR_BGR2GRAY)
        image = np.tile(image[:, :, np.newaxis], [1, 1, 3])
        data = extractor(outputs)
        image_vis = visualizer.visualize(image, data)
        entry_idx = context["entry_idx"] + 1
        out_fname = cls._get_out_fname(entry_idx, context["out_fname"])
        out_dir = os.path.dirname(out_fname)
        if len(out_dir) > 0 and not os.path.exists(out_dir):
            os.makedirs(out_dir)
        cv2.imwrite(out_fname, image_vis)
        logger.info(f"Output saved to {out_fname}")
        context["entry_idx"] += 1

    @classmethod
    def postexecute(cls: type, context: Dict[str, Any]):
        pass

    @classmethod
    def _get_out_fname(cls: type, entry_idx: int, fname_base: str):
        base, ext = os.path.splitext(fname_base)
        return base + ".{0:04d}".format(entry_idx) + ext

    @classmethod
    def create_context(cls: type, args: argparse.Namespace, cfg: CfgNode) -> Dict[str, Any]:
        vis_specs = args.visualizations.split(",")
        visualizers = []
        extractors = []
        for vis_spec in vis_specs:
            texture_atlas = get_texture_atlas(args.texture_atlas)
            vis = cls.VISUALIZERS[vis_spec](cfg=cfg, texture_atlas=texture_atlas)
            visualizers.append(vis)
            extractor = create_extractor(vis)
            extractors.append(extractor)
        visualizer = CompoundVisualizer(visualizers)
        extractor = CompoundExtractor(extractors)
        context = {
            "extractor": extractor,
            "visualizer": visualizer,
            "out_fname": args.output,
            "entry_idx": 0,
        }
        return context


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=DOC,
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=120),
    )
    parser.set_defaults(func=lambda _: parser.print_help(sys.stdout))
    subparsers = parser.add_subparsers(title="Actions")
    for _, action in _ACTION_REGISTRY.items():
        action.add_parser(subparsers)
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    verbosity = args.verbosity if hasattr(args, "verbosity") else None
    global logger
    logger = setup_logger(name=LOGGER_NAME)
    logger.setLevel(verbosity_to_level(verbosity))
    args.func(args)


if __name__ == "__main__":
    main()
