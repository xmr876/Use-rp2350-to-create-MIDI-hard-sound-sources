"""
gmss — SF2 → 板载 GMSS 音色库转换工具链

模块划分：
    riff.py      RIFF 容器解析（sf2 外壳）
    sf2defs.py   SF2 记录结构、生成器枚举、单位换算
    parse.py     hydra 记录 → Python 对象
    (后续) plan.py      GM 映射与 zone 规划
    (后续) encode.py    PCM16 / IMA-ADPCM 编码
    (后续) layout.py    跨片交错布局与镜像输出
    (后续) flash.py     串口刷写协议客户端
"""
__version__ = "0.1.0"
