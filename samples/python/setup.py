# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import argparse
import utils.install as install

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--qnn-sdk-version", default="2.24", type=str)
    parser.add_argument("--lib-version", default="arm64x-windows-msvc", type=str)
    parser.add_argument("--dsp-arch", default=73, type=int)
    args = parser.parse_args()

    qnn_sdk_version = args.qnn_sdk_version
    lib_version = args.lib_version
    dsp_arch = args.dsp_arch

    start_number = 140
    print()
    print(start_number * "*")
    print("*   You can press [Ctrl+C] to interrupt the current task. If the downloading is interrupted, you can re-execute this script to continue.   *")
    print("*                                               [Support Resume Broken Download]                                                           *")
    print(start_number * "*")
    print()

    try:
        install.install_tools()
        install.install_qai_sdk(qnn_sdk_version)
        install.install_qai_appbuilder(qnn_sdk_version, lib_version)
        install.setup_qai_env(qnn_sdk_version, lib_version, dsp_arch)

        print()
        print(start_number * "*")
        print("*                                                   [Installation Succeeded.]                                                              *")
        print(start_number * "*")
        print()
    except KeyboardInterrupt:
        pass