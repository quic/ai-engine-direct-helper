# QAI AppBuilder (Quick AI Application Builder)

## Introduction

#### QAI AppBuilder
Quick AI Application Builder(this repository) is also referred to as *QAI AppBuilder* in the source and documentation. QAI AppBuilder is extension for Qualcomm® AI Runtime SDK. We need some libraries in Qualcomm® AI Runtime SDK for using QAI AppBuilder. <br>
QAI AppBuilder is designed for developer to using Qualcomm® AI Runtime SDK to execute model on Windows on Snapdragon(WoS) and Linux platforms easily. We encapsulated Qualcomm® AI Runtime SDK APIs to several simple APIs for loading the models to CPU and HTP and executing inference.

#### Qualcomm® AI Runtime SDK

Qualcomm® AI Runtime SDK is designed to provide unified, low-level APIs for AI development. It can be downloaded from Qualcomm software center:<br>
https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK <br>
Or from QPM [this option expected to be deprecated soon]<br>
https://qpm.qualcomm.com/#/main/tools/details/Qualcomm_AI_Runtime_SDK

## Advantage

Developers can use QAI AppBuilder in both C++ and Python projects <br>

• Support both C++ & Python <br>
• Support both Windows & Linux. <br>
• Support Genie(LLM) [*NEW!*]. <br>
• Support Multi Graph [*NEW!*]. <br>
• Support multiple models. <br>
• Support multiple inputs & outputs. <br>
• Easier for developing apps. <br>
• Faster for testing models. <br>
• Plenty of sample code. <br>

** Support ARM64 Windows, Linux and Ubuntu (e.g.: X Elite Windows, QCS8550 Linux and QCM6490 Ubuntu)*

## Environment Setup
Refere to [python.md](docs/python.md) for instructions on setting up the Python(x64 version) environment to use QAI AppBuilder on Windows on Snapdragon (WoS) platforms.

## Samples
We have several [samples](samples/) which can be run directly:<br>
1. [Sample code](samples/python/README.md): Guide to run several [AI-Hub](https://aihub.qualcomm.com/compute/models) models throug sample code.
2. OpenAI Compatibility API Service(LLM Service):<br>
2.1 [Python based service](samples/genie/python/README.md): Guide to run OpenAI compatibility API services developed with python.<br>
2.2 [C++ based service](samples/genie/c++/README.md): Guide to run OpenAI compatibility API services developed with C++.<br>
3. [WebUI samples](samples/webui/README.md): Guide to run several WebUI based AI applications.

## Components
There're two ways to use QAI AppBuilder:
### 1. Using the QAI AppBuilder C++ libraries to develop C++ based AI application.
Download prebuild binary package *QAI_AppBuilder-win_arm64-{Qualcomm® AI Runtime SDK version}-Release.zip* to get these files: https://github.com/quic/ai-engine-direct-helper/releases

### 2. Using the QAI AppBuilder Python binding extension to develop Python based AI application.
Download Python extension *qai_appbuilder-{version}-cp312-cp312-win_amd64.whl* and install it with the command below:
https://github.com/quic/ai-engine-direct-helper/releases

```
pip install qai_appbuilder-{version}-cp312-cp312-win_amd64.whl
```

## User Guide
Refere to [User Guide](docs/user_guide.md) on how to use QAI AppBuilder to program AI application.

## Build
Build QAI AppBuilder from source with Visual Studio 2022 on WoS device:<br>
- Install Visual Studio 2022: 
  - https://docs.qualcomm.com/bundle/publicresource/topics/80-62010-1/Install-Visual-Studio-2022.html?product=Windows%20on%20Snapdragon
- Install x64 version Python-3.12.6: 
  - https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
- Use the commands below to install Python dependency: 
```
pip install wheel setuptools pybind11
```
- Clone this repository to local: 
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```
- If you have cloned it before, you can update the code by the following command:
```
cd ai-engine-direct-helper
git pull --recurse-submodules
```
- Set environment 'QNN_SDK_ROOT' to the Qualcomm® AI Runtime SDK path which you're using. E.g.:
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
```
- Use the commands below to build and install Python extension(*.whl): 
```
cd ai-engine-direct-helper
python setup.py bdist_wheel

# Install the extension:
pip install dist\qai_appbuilder-2.34.0-cp312-cp312-win_amd64.whl
```

## License
QAI AppBuilder is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.
