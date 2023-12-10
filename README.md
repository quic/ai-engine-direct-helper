# Python bindings for Qualcomm® AI Engine Direct

## What's Qualcomm® AI Engine Direct?

Qualcomm® AI Engine Direct is designed to provide unified, low-level APIs for AI development. Qualcomm® AI Engine Direct is also referred to as *QNN* in the source and documentation.

## Introduction

This repository is an extension that is developed based on QNN SDK. We need some libraries in QNN SDK while using QNNHelper. The QNN SDK can be downloaded here: https://qpm.qualcomm.com/#/main/tools/details/qualcomm_ai_engine_direct

## QNNHelper 

QNNHelper is designed for developer to using QNN SDK to execute model on Windows on Snapdragon(WoS) platforms more easily. We encapsulated QNN SDK APIs to several simple APIs into QNNHelper for loading the models to HTP, running inference and releasing the resource.

**libqnnhelper.dll {libqnnhelper.lib, libQNNHelper.hpp}** –– C++ projects can use this lib to run models in HTP.

**qnnhelper{version}.pyd** –– Python extension. Easier to develop QNN based GUI app for WoS.

**SvcQNNHelper.exe** –– Due to HTP limitations, we can only load models smaller than 4GB in one process. This app is used to help us load the models in new processes(Multiple processes can be created) and inference to avoid HTP restrictions.

## Advantage

Developers can use these helper libraries for python to call functions within the SDK. <br>

• Support both C++ & Python <br>
• Support multiple models. <br>
• Support multiple inputs & outputs. <br>
• Support multiple processes. <br>
• Easier for developing apps. <br>
• Faster for testing models. <br>

Using these Python extensions with ARM64 Python will make it easier for developers to build GUI app for Windows on Snapdragon(WoS) platforms. Python ARM64 version has support for following modules: PyQt6, OpenCV, Numpy, PyTorch*, Torchvision*, ONNX*. Developers can design apps that benefit from rich Python ecosystem. <br>

**PyTorch, Torchvision, ONNX: need to compile from source code.*

## User Guide
Please refere to 
[User Guide](docs/User%20Guide.md) on how to use QNNHelper in your project.

## License
QNNHelper is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.
