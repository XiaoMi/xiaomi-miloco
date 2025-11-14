# User Guide

## Regarding Xiaomi Miloco

**Xiaomi Miloco**(Xiaomi Local Copilot) is a future exploration solution for smart homes. Using Xiaomi Home cameras as the source of visual information and a self-developed LLM as its core, it connects all IoT devices throughout the house. Based on the development paradigm of LLM, it enables users to define various family needs and rules in natural language, achieving broader and more creative smart device integration.

- Ask questions anytime. For example: "[Check what's in the frame](https://www.bilibili.com/video/BV1fwCsBgEih/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)", "Find where my cat is"

- Create rules. For example: "[Turn on the desk lamp when someone is reading]( https://www.bilibili.com/video/BV1zwCsBgEMc/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)", "Send a Xiaomi Home notification reminder when someone is using their phone"

- Complex controls. For example: "Check if the child is doing homework - if not, have the smart speaker announce 'Time to do homework'", "[set the color of the light strip based on the color of the down jacket in the image.](https://www.bilibili.com/video/BV1zwCsBgEv2/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)"

- Dynamic reasoning. For example: "[When people are detected in the frame, generate compliments based on their dressing style and broadcast them through the smart speaker](https://www.bilibili.com/video/BV1BACsBzEUZ/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)", "When someone is sleeping in the frame, adjust the air conditioner temperature based on whether they're covered with a blanket"

## Basic Configuration
Before you begin, follow the tutorial below to complete the basic setup.
### Login Code Setup
- Set a 6-digit login code, which can include a combination of numbers and letters. Be sure to remember it, as it will be required when logging in from a new environment.
### Authorization
- Xiaomi Account Authorization: During the initial login, authorize via your Xiaomi Account to retrieve and control Xiaomi Home devices and scenes.
- HA Authorization: On the Settings > Authorization page, configure authorization using Token + API to retrieve and execute Home Assistant scenes.

  <img src="../../assets/images/HA_Authorization.PNG" width="60%" />

### Model Settings
- Configure and manage models in the Model Management section:
  - The planning LLM is used for Query intent analysis, task planning, and tool invocation. Cloud-based models are recommended.
  - The visual understanding LLM is used for processing and interpreting camera visual data. The local Xiaomi MiMo-VL-Miloco 7B model is recommended.

    <img src="../../assets/images/model_management.jpg" width="60%" />

## Start
### How to Create Rules
#### Rule Card Overview
Rule Name: Required. Choose an appropriate name for the current rule.
Select Active Camera(s): Required, supports single or multiple selection. The selected camera(s) will serve as the visual source for triggering condition analysis in the current rule.
Trigger Condition: Required. Use natural language to describe the trigger condition. The recommended句式 is "When...". The trigger condition must be clearly and accurately expressed.
Execution Action: Required. The action(s) to be executed when the condition is triggered. Supports single or multiple simultaneous actions.
1. MCP: Select the MCP service(s) required for the rule trigger. Supports single or multiple selection.
2. Device Control: Custom execution actions can be input. If this option is selected, the "Test Generate Instruction" must be clicked before saving. You can choose whether to cache the instruction.
    - Cache: When the rule is triggered, the command cached during testing will be executed. For example: "Turn on the desk lamp." If desk lamp A was turned on during the test, desk lamp A will be turned on every time the rule is subsequently triggered. The process is fast and takes less time.
    - Do not cache: When the rule is triggered, it will perform real-time inference and execution. For example: "Adjust the color of the light according to the color of the person's clothes in the picture." The subsequent execution will be based on the color inferred in real-time from the picture. This process takes longer; choose according to your needs.
3. Automation Scenes: You can select Xiaomi Home or HA scenes to execute. Supports single or multiple selection.
4. Send  Xiaomi Home Notification: Supports customizing Xiaomi Home notification content and sending it.

    <img src="../../assets/images/Rule_Card.PNG" width="40%" />


**Advanced Configuration**

Optional settings that allow you to specify the time period and minimum interval for rule triggers.
1. Trigger Time Period:
You can select from three options:
    - All Day
    - Daytime
    - Nighttime
2. Minimum Trigger Interval:
Set the minimum time interval required between rule triggers.

    <img src="../../assets/images/Minimum_Trigger_Interval.png" width="60%" />

#### Method 1: Manually Create Rules
On the Rules Management page, click "Add Rule" and fill in the required rule details as needed. [Reference video](https://www.bilibili.com/video/BV1BACsBzEUZ/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c) 

#### Method 2: Automatically Create Rules
Implementation: In the AI Hub, describe rule-based requirements using natural language in the dialog box. The LLM will invoke the rule creation Agent to automatically generate the rule.
1. Select the camera(s) for which the rule should be active. If no selection is made, the LLM will automatically choose by default. Then, select the MCP service(s) required for the rule. After configuration, describe your rule-based requirement in natural language and click "Send".
2. The LLM will analyze the user's Query, invoke the rule creation Agent, and automatically populate the rule card. Users can modify the content of the auto-generated rule card if needed. After verifying the card content, click "Save Rule" to successfully create the rule.
3. [Reference video](https://www.bilibili.com/video/BV1zwCsBgEMc/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)

### How to Configure Models
#### Add Cloud Models
1. On the Model Management page, click the "Add Model" button to open the cloud model configuration pop-up.
2. After entering the Base URI and API Key, the model list will be automatically fetched. Some cloud models do not support automatic list retrieval and require manual entry of the model name.
3. Click "Confirm" to save the settings.
#### Adding Local Models
The startup script will automatically download and configure the default model. You can also customize models using the following method:
1. Modify the models configuration in the config/ai_engine_config.yaml file by adding/editing custom model names and configuration items. The model_path (mmproj_path) should point to the absolute address of the local model file.
2. Restart the inference engine service.
#### Advanced Settings
- The planning LLM is used for Query intent analysis, task planning, and tool invocation. Using cloud-based models will deliver better performance.
- The visual understanding LLM is used for processing and interpreting camera visual data. Xiaomi's self-developed on-device model is recommended.
### How to Configure MCP Services
1. After completing authorization, you can use three pre-packaged MCP capabilities: MIoT Automation, MIoT Device Control, and HA Automation. Other third-party MCP services require users to develop and configure them independently.
2. On the MCP services page, you can use the "Add Service" function to integrate three types of MCP services: SSE, HTTP, and Stdio. Once configured, these MCP services can be selected and used in AI Center conversations.
3. Example: Select the MCP service type, enter the name, URL, and request headers to successfully connect to a weather MCP. During conversations, selecting the weather MCP allows you to perform weather queries.

    <img src="../../assets/images/MCP_Services.png" width="40%" />  <img src="../../assets/images/Use_MCP_Services.png" width="40%" />

