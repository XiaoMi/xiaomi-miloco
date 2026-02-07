1、为 RTSP 摄像头添加动态配置，支持在线添加 RTSP 流，基于项目内置的 OpenCV 视频解析库实现。
 <img src="assets/images/devicemanager.png"/>

2、增强 Home Assistant 功能，内置 HA 设备控制 MCP 功能，集成 WS API，配置 HA 令牌后自动启动 WS 连接，并在设备管理中显示 HA 设备信息和设备实体。
 <img src="assets/images/hadevice.png"/>
  <img src="assets/images/hadevice-use.png" width="50"/>

3、新增基于向量数据库的长期记忆功能，支持对话的自主记忆和手动添加记忆，带来更人性化的用户体验。
 <img src="assets/images/mem.png"/>

4、在使用在线视觉模型时，增加图像压缩功能以节省token
5、新增小爱音箱接入功能，支持语音接管AI对话控制，刷机请参考项目[open-xiaoai](https://github.com/idootop/open-xiaoai)，只需要刷机启动客户端即可，服务端已使用py重写并集成进来了
 <img src="assets/images/xiaoai.png"/>
