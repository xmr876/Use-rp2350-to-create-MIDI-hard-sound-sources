# archive/ — 已废弃的实现，保留作为设计演进记录

这些文件**不参与编译**，留着是为了记住"为什么当初那样做、后来为什么放弃"。

## flash_direct.c / flash_direct.h

**QMI Direct Serial Mode 多片 Flash 驱动**（v1 方案的产物）。

原设计：裸片 RP2350A + 4 片 16MB QSPI Flash，
用 QMI 的 CS0/CS1 硬件片选 + CS2/CS3 软件片选，采样交错分布到各片。

**为什么放弃：**

1. **最终硬件改成了 Pico 2 模块**，而 RP2350 的 QSPI 引脚是**专用引脚**
   （不像 RP2040 那样与 GPIO0~5 复用），在 Pico 2 上直接连到板载 Flash，
   **没有引到排针** —— 载板根本碰不到 QMI 总线。
2. 就算能碰到，QMI Direct 模式有三条致命约束：
   - 期间**不能执行 flash 里的代码**（所有相关函数必须 `__not_in_flash_func`）
   - 期间不能触发任何 flash 里的中断处理函数
   - 每次操作前必须轮询 `BUSY == 0`
3. 最终方案（Pico 2 板载 Flash 魔改 16MB）让音色库**直接内存映射**，
   读采样点就是读指针，上面这些约束一条都不存在了。

**保留价值**：里面的 QMI 寄存器位域用法（`direct_csr` 的
`ASSERT_CS0N/CS1N`、`TXFULL/RXEMPTY` 轮询、`NODATA` 等）
如果将来真要做多片 QSPI，是可以直接抄回去的。
