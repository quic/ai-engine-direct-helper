# AI Engine Direct Helper 

## Introduction
Qualcomm® AI Engine Direct is designed to provide unified, low-level APIs for AI development. Qualcomm® AI Engine Direct is also referred to as *QNN* in the source and documentation. The QNN SDK can be downloaded here: https://qpm.qualcomm.com/#/main/tools/details/qualcomm_ai_engine_direct

AI Engine Direct Helper(this repository) is also referred to as *QNNHelper* in the source and documentation. QNNHelper is extension for QNN SDK. We need some libraries in QNN SDK for using QNNHelper. <br>
QNNHelper is designed for developer to using QNN SDK to execute model on Windows on Snapdragon(WoS) platforms more easily. We encapsulated QNN SDK APIs to several simple APIs into QNNHelper for loading the models to CPU or HTP, running inference and releasing the resource.

## Advantage

Developers can use these helper libraries for C++ & Python to call functions within the SDK. <br>

• Support both C++ & Python <br>
• Support multiple models. <br>
• Support multiple inputs & outputs. <br>
• Support multiple processes. <br>
• Easier for developing apps. <br>
• Faster for testing models. <br>

Using the Python extensions with ARM64 Python will make it easier for developers to build GUI app for Windows on Snapdragon(WoS) platforms. Python 3.11.5 ARM64 version has support for following modules: PyQt6, OpenCV, Numpy, PyTorch*, Torchvision*, ONNX*. Developers can design apps that benefit from rich Python ecosystem. <br>

**PyTorch, Torchvision, ONNX, ONNX Runtime: need to compile from source code.*

## Components
There're two ways to use QNNHelper:
### 1. Using the QNNHelper C++ libraries to develop C++ based AI application.
Download prebuild binary package *QNNHelper-win_arm64-{QNN SDK version}-Release.zip* to get these files. E.g: https://github.com/quic/ai-engine-direct-helper/releases/download/v2.19.0/QNNHelper-win_arm64-QNN2.19.0-Release.zip

**libqnnhelper.dll {libqnnhelper.lib, LibQNNHelper.hpp}** –– C++ projects can use this lib to run models in HTP.
**SvcQNNHelper.exe** –– Due to HTP limitations, we can only load models smaller than 4GB in one process. This app is used to help us load the models in new processes(Multiple processes can be created) and inference to avoid HTP restrictions.

### 2. Using the QNNHelper Python binding extension to develop Python based AI application.
Download Python extension *qnnhelper-{version}-cp311-cp311-win_arm64.whl* and install it with the command below. E.g: https://github.com/quic/ai-engine-direct-helper/releases/download/v2.19.0/qnnhelper-2.19.0-cp311-cp311-win_arm64.whl

```
pip install qnnhelper-2.19.0-cp311-cp311-win_arm64.whl
```

## User Guide
Please refere to [User Guide](Docs/User_Guide.md) on how to use QNNHelper in your project.

## Build
Build project with Visual Studio 2022 on WoS device:<br>
- Set environment 'QNN_SDK_ROOT' to the QNN SDK path which you're using. E.g.: QNN_SDK_ROOT = "C:\Qualcomm\AIStack\QNN\2.18.0.240101\"
- Install Visual Studio 2022: 
  - https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/Install-Visual-Studio-2022.html?product=Windows%20on%20Snapdragon
- Install Python-3.11.5 ARM64: 
  - https://www.python.org/ftp/python/3.11.5/python-3.11.5-arm64.exe
- Download pybind11 repository to 'ai-engine-direct-helper\PyQNNHelper\pybind11': 
  - https://github.com/pybind/pybind11.git
- Use the commands below to build and install Python extension(*.whl): 
```
cd C:\Source\ai-engine-direct-helper
python setup.py bdist_wheel

# Install the extension:
pip install dist\qnnhelper-0.1.0-cp311-cp311-win_arm64.whl
```

## License
QNNHelper is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.
