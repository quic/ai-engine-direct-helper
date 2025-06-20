<br><br><br>

<div align="center">
  <a href="README.md"><img src="docs/images/qai_appbuilder.png" alt="Quick AI Application Builder" width="360" height="90"></a>
</div>

<br><br><br>

<div align="center">
  <h3>A simple way to build AI application based on Qualcomm® AI Runtime SDK.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
  <a href="https://github.com/quic/ai-engine-direct-helper"><img src="https://img.shields.io/github/stars/quic/ai-engine-direct-helper" alt="stars"></a>
  <a href="https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.34.0"><img src="https://img.shields.io/badge/Release-v2.34.0-green" alt="Release"></a>
  <a href="https://opensource.org/license/BSD-3-clause"><img src="https://img.shields.io/badge/License-BSD--3--Clause-blue" alt="License: BSD 3-Clause"></a>
  <a href="https://www.python.org/downloads/windows/"><img src="https://img.shields.io/badge/Python-00599C?logo=Python" alt="Python"></a>
  <a href="https://en.cppreference.com/w/cpp/compiler_support"><img src="https://img.shields.io/badge/C++-999999?logo=c%2B%2B" alt="C++"></a>
  <a href="https://www.qualcomm.com/products/technology/processors/ai-engine"><img src="https://img.shields.io/badge/NPU-ccffff" alt="AI"></a>
  <a href="https://github.com/quic/ai-hub-apps/tree/main/tutorials/llm_on_genie"><img src="https://img.shields.io/badge/Genie AI-ffff9C" alt="AI"></a>
</div>
<br>

---

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction

#### QAI AppBuilder
The Quick AI Application Builder (this repository) is also referred to as "QAI AppBuilder" in the source code and documentation.<br>
QAI AppBuilder is an extension of the Qualcomm® AI Runtime SDK, which is used to simplify the deployment of QNN models. Some libraries from the Qualcomm® AI Runtime SDK are required to use QAI AppBuilder.<br>
QAI AppBuilder is designed to help developers easily use the Qualcomm® AI Runtime SDK to execute models on Windows on Snapdragon (WoS) and Linux platforms. It encapsulates the model execution APIs into a set of simplified interfaces for loading models onto the NPU/HTP and performing inference. It substantially lowers the complexity of model deployment for developers.

#### Qualcomm® AI Runtime SDK

Qualcomm® AI Runtime SDK is designed to provide unified, low-level APIs for AI development. It can be downloaded from Qualcomm software center:<br>
https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK <br>
Or from QPM [this option expected to be deprecated soon]<br>
https://qpm.qualcomm.com/#/main/tools/details/Qualcomm_AI_Runtime_SDK

## Advantage

Developers can use QAI AppBuilder in both C++ and Python projects <br>

• Support both C++ & Python <br>
• Support both Windows & Linux <br>
• Support Genie(Large Language Model) [*NEW!*] <br>
• Support Multi Graph [*NEW!*] <br>
• Support LoRA <br> 
• Support multiple models <br>
• Support multiple inputs & outputs <br>
• Easier for developing apps <br>
• Faster for testing models <br>
• Plenty of sample code <br>

** Support ARM64 Windows, Linux and Ubuntu (e.g.: X Elite Windows, QCS8550 Linux and QCM6490 Ubuntu). <br>
** Support OpenAI Compatible API Service([GenieAPIService](samples/genie/c++/README.md#run-the-service-on-mobilesnapdragon-8-elite-mobile-device-)) on WoS, Android and Linux.

## Diagram
<br>
<div align="center">
  <a href="README.md"><img src="docs/images/diagram.png" alt="Quick AI Application Builder" width="777" height="456"></a>
</div>

## Environment Setup
Refere to [python.md](docs/python.md) for instructions on setting up the Python(x64 version) environment to use QAI AppBuilder on Windows on Snapdragon (WoS) platforms. <br>

## WebUI AI Application
We have developed several [WebUI AI applications](samples/webui/README.md) based on QAI AppBuilder, allowing you to experience them quickly. <br>
All these applications run on a local PC, requiring *no internet connection* and are *completely free*. <br><br>
<a href="docs/python.md"><img src="https://img.shields.io/badge/Note: - Before trying other functions, we suggest that you try these WebUI AI Application first.-important "></a>

|  App   | Description  |
|  ----  | :----    |
| ImageRepairApp | An image restoration tool designed to repair old or damaged photographs. |
| StableDiffusionApp  | A text-to-image generation tool that creates images based on user input. |
| GenieWebUI  | A large language model (LLM) interface that enables interactive conversations.|

## OpenAI Compatible API Service (GenieLLMService):<br>
Considering that the current mainstream method for invoking LLMs is based on OpenAI-compatible APIs, we have implemented such interfaces in both C++ and Python. This allows application developers to interact with the local large language model running on NPU in a familiar way. <br>
Many third-party applications that support the OpenAI API can seamlessly switch to the local NPU-based model by simply changing the API IP endpoint. <br>
We have also implemented the client sample code of Genie API Service through both C++ and Python for the reference of developers.

1. [Python based service](samples/genie/python/README.md): Guide to run OpenAI compatible API services developed with python.<br>
2. [C++ based service](samples/genie/c++/README.md): Guide to run OpenAI compatible API services developed with C++.<br>

## Samples
We have many [samples](samples/python/README.md) which can be run directly. The models used in these samples are from [AI-Hub](https://aihub.qualcomm.com/compute/models).<br>
*We suggest that you try to run these sample code before you actually start writing your own code.*<br>

## Tools
We have [LangFlow Package](tools/langflow/) for running QNN model in 3rd party [LangFlow](https://github.com/langflow-ai/langflow) framework.

## QAI AppBuilder Components
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
You can use the pre-compiled version directly and download the version you need from [Release](https://github.com/quic/ai-engine-direct-helper/releases). But if you really want to compile it yourself, you can refer to the [BUILD.md](BUILD.md).

## License
QAI AppBuilder is licensed under the BSD 3-clause "New" or "Revised" License. Check out the [LICENSE](LICENSE) for more details.

## Star History

<a href="https://www.star-history.com/#quic/ai-engine-direct-helper&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=quic/ai-engine-direct-helper&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=quic/ai-engine-direct-helper&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=quic/ai-engine-direct-helper&type=Date" />
 </picture>
</a>