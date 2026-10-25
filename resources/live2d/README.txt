DeskPet 角色渲染器资源
========================

这个目录里是"把角色画到屏幕上"用的只读文件，不含任何模型。
你自己的 Live2D 模型仍然放在用户数据目录的 live2d/ 下，程序只读、不改、不上传。

文件说明
--------
renderer.html          渲染页面外壳
pet_render.js          DeskPet 自己写的最小 WebGL 渲染器（约 300 行，无第三方依赖）
pet_page.js            页面控制器：自适应窗口、导出命中轮廓、回传鼠标事件
live2dcubismcore.min.js  Live2D Cubism Core（官方运行时，见下方许可）

关于 live2dcubismcore.min.js
---------------------------
来源：Live2D 官方 SDK for Web（CubismSdkForWeb-5-r.5）中的 Core/live2dcubismcore.min.js
版本：Cubism Core 06.00.0001
许可：Live2D Proprietary Software License Agreement
      https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html

Live2D 官方在 Core/RedistributableFiles.txt 中允许随应用再分发以下三个文件：
    live2dcubismcore.js
    live2dcubismcore.min.js
    live2dcubismcore.d.ts
本目录只放了其中的 min 版本，属于被允许再分发的范围。

注意：Core 版本必须不低于模型 moc3 的版本，否则读不了模型
（例如 Cubism 5 导出的 moc3 需要 Cubism Core 5.x 以上）。
更新 Core 时请从 Live2D 官方渠道获取，不要从第三方站点下载。

DeskPet 不打包、不下载、不分发任何 Live2D 模型文件。
