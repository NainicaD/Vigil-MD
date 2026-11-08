"""Runnable pipeline stages, numbered in run order (s01 -> s09).

Each stage is a script: `python pipeline/s01_build_label_set.py --help`. The rules
they apply live in vigil_model/, never here. Unnumbered files are side analyses that
the training chain does not depend on.
"""
