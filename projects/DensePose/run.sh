#!/bin/sh

python apply_net.py dump configs/densepose_rcnn_R_50_FPN_s1x.yaml DensePose_ResNet50_FPN_s1x.pkl images --output out/dump.pkl -v
