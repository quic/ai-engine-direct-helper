# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import argparse
import utils.install as install

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--qnn-sdk-version", default=install.DEFAULT_SDK_VER, type=str)
    parser.add_argument("--lib-arch", default="arm64x-windows-msvc", type=str)
    parser.add_argument("--dsp-arch", default=73, type=int)
    args = parser.parse_args()

    qnn_sdk_version = args.qnn_sdk_version
    lib_arch = args.lib_arch
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

        if qnn_sdk_version == "both":
            qnn_sdk_version = "2.34"
            install.install_qai_sdk(qnn_sdk_version)
            install.install_qai_appbuilder(install.DEFAULT_SDK_VER)
            install.setup_qai_env(qnn_sdk_version, lib_arch, dsp_arch, "qai_libs")

            qnn_sdk_version = "2.24"
            install.install_qai_sdk(qnn_sdk_version)
            install.setup_qai_env(qnn_sdk_version, lib_arch, dsp_arch, "qai_libs_2.24")

        else:
            install.install_qai_appbuilder(qnn_sdk_version)

            try:
                install.install_qai_runtime(qnn_sdk_version, lib_arch, dsp_arch, "qai_libs")
            except Exception as e:
                print(e)
                print("Failed to install QAIRT runtime libraries. Try to install QAIRT SDK.")
                install.install_qai_sdk(qnn_sdk_version)
                install.setup_qai_env(qnn_sdk_version, lib_arch, dsp_arch, "qai_libs")

        print()
        print(start_number * "*")
        print("*                                                   [Installation Succeeded.]                                                              *")
        print(start_number * "*")
        print()
    except KeyboardInterrupt:
        pass
