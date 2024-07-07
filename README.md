# AI Engine Direct Helper 

## [ <font color=red>We used a new name 'QAI AppBuilder' instead of 'qnnhelper' ]</font>

## Introduction
#### QNN SDK
Qualcomm® AI Engine Direct is designed to provide unified, low-level APIs for AI development. Qualcomm® AI Engine Direct is also referred to as *QNN* in the source and documentation. The QNN SDK can be downloaded from Qualcomm software center:<br>
https://softwarecenter.qualcomm.com/#/catalog/item/a0844287-db23-11ed-a260-063166a9270b?type=Tool <br>
Or from QPM [this option expected to be deprecated soon]<br>
https://qpm.qualcomm.com/#/main/tools/details/qualcomm_ai_engine_direct

#### QAI AppBuilder
AI Engine Direct Helper(this repository) is also referred to as *QAI AppBuilder* in the source and documentation. QAI AppBuilder is extension for QNN SDK. We need some libraries in QNN SDK for using QAI AppBuilder. <br>
QAI AppBuilder is designed for developer to using QNN SDK to execute model on Windows on Snapdragon(WoS) platforms easily. We encapsulated QNN SDK APIs to several simple APIs for loading the models to CPU or HTP and executing inference.

## Advantage

Developers can use QAI AppBuilder in both C++ and Python projects <br>

• Support both C++ & Python <br>
• Support multiple models. <br>
• Support multiple inputs & outputs. <br>
• Easier for developing apps. <br>
• Faster for testing models. <br>

Using the Python extensions with ARM64 Python will make it easier for developers to build GUI app for Windows on Snapdragon(WoS) platforms. Python 3.11.5 ARM64 version has support for following modules: PyQt6, OpenCV, Numpy, PyTorch*, Torchvision*, ONNX*. Developers can design apps that benefit from rich Python ecosystem. <br>

**PyTorch, Torchvision, ONNX, ONNX Runtime: need to compile from source code.*
**Also support x64 Python.*

## Components
There're two ways to use QAI AppBuilder:
### 1. Using the QAI AppBuilder C++ libraries to develop C++ based AI application.
Download prebuild binary package *QAI_AppBuilder-win_arm64-{QNN SDK version}-Release.zip* to get these files: https://github.com/quic/ai-engine-direct-helper/releases

**libappbuilder.dll {libappbuilder.lib, LibAppBuilder.hpp}** –– C++ projects can use this lib to run models in HTP.
**QAIAppSvc.exe** –– Due to HTP limitations, we can only load models smaller than 4GB in one process. This app is used to help us load the models in new processes(Multiple processes can be created) and inference to avoid HTP restrictions.

### 2. Using the QAI AppBuilder Python binding extension to develop Python based AI application.
Download Python extension *qai_appbuilder-{version}-cp311-cp311-win_arm64.whl* and install it with the command below:
https://github.com/quic/ai-engine-direct-helper/releases

```
pip install qai_appbuilder-2.24.0-cp311-cp311-win_arm64.whl
```

## User Guide
Please refere to [User Guide](docs/user_guide.md) on how to use QAI AppBuilder in your project.

## Build
Build project with Visual Studio 2022 on WoS device:<br>
- Set environment 'QNN_SDK_ROOT' to the QNN SDK path which you're using. E.g.: Set QNN_SDK_ROOT = "C:\\Qualcomm\\AIStack\\QAIRT\\2.24.0.240626\\"
- Install Visual Studio 2022: 
  - https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/Install-Visual-Studio-2022.html?product=Windows%20on%20Snapdragon
- Install Python-3.11.5 ARM64: 
  - https://www.python.org/ftp/python/3.11.5/python-3.11.5-arm64.exe
- Use the commands below to install Python dependency: 
```
pip install wheel setuptools pybind11
```
- Download pybind11 repository to 'ai-engine-direct-helper\pybind\pybind11': 
  - https://github.com/pybind/pybind11.git
- Use the commands below to build and install Python extension(*.whl): 
```
cd C:\Source\ai-engine-direct-helper
python setup.py bdist_wheel

# Install the extension:
pip install dist\qai_appbuilder-2.24.0-cp311-cp311-win_arm64.whl
```

## License
QAI AppBuilder is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.
