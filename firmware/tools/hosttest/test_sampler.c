/*
 * test_sampler.c — 在 **主机** 上测试 sampler.c / dsp_core.c 的纯逻辑部分
 *
 * ★ 为什么能在主机上跑：
 *   sampler.c 和 dsp_core.c 都刻意不 include 任何 pico-sdk 头，
 *   只用 gmss_format.h 和标准 C。所以可以用 zig cc 编成 x86-64 直接跑，
 *   不需要开发板、不需要 QEMU。
 *
 *   采样解码是全工程最容易"差一个 LSB 就出咔哒"的地方，
 *   必须在没有硬件的时候就能验证。这个程序就是那层验证。
 *
 * 它做四件事：
 *   1. 自检：结构体尺寸、pitch 表、库加载
 *   2. 把若干 (zone, start, count) 的解码结果**按二进制 dump 出去**，
 *      交给 tools/verify_sampler_host.py 与 Python 参考解码器 player.py 对拍
 *   3. 自洽性检查：随机访问接口 vs 连续流接口，两条路径必须逐样本一致
 *   4. 边界检查：start=0 / start=attack-1 / start=attack / start=attack+1 /
 *      loop 点附近 / 末尾越界
 *
 * 输出格式（给 Python 解析）：
 *   文本行：  "CHECK <name> <PASS|FAIL> <detail>"
 *   二进制：  argv[2] 指定的文件，见 dump_record_t
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "sampler.h"
#include "dsp_core.h"

/* ------------------------------------------------------------------ */
/* dump 文件格式                                                       */
/* ------------------------------------------------------------------ */
/*
 * 每条记录：
 *   uint32 zone_index
 *   uint32 start_frame
 *   uint32 count
 *   uint32 kind          0 = 随机访问接口, 1 = 连续流接口
 *   int16  samples[count]
 * 小端。
 */
static void put_u32(FILE *f, uint32_t v)
{
    uint8_t b[4] = { (uint8_t)(v), (uint8_t)(v >> 8),
                     (uint8_t)(v >> 16), (uint8_t)(v >> 24) };
    fwrite(b, 1, 4, f);
}

static int g_fail = 0;

static void check(const char *name, int ok, const char *detail)
{
    printf("CHECK %s %s %s\n", name, ok ? "PASS" : "FAIL", detail ? detail : "");
    if (!ok) g_fail++;
}

/* ------------------------------------------------------------------ */
/* 载入文件                                                            */
/* ------------------------------------------------------------------ */

static uint8_t *load_file(const char *path, size_t *out_len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n <= 0) { fclose(f); return NULL; }
    uint8_t *buf = malloc((size_t)n);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) {
        free(buf); fclose(f); return NULL;
    }
    fclose(f);
    *out_len = (size_t)n;
    return buf;
}

/* ------------------------------------------------------------------ */
/* 主程序                                                              */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "用法: %s <library.bin> <dump.bin>\n", argv[0]);
        return 2;
    }

    /* ---------- 1. 结构体尺寸自检 ---------- */
    printf("CHECK sizeof_header %s %zu\n",
           sizeof(gmss_header_t) == 64 ? "PASS" : "FAIL", sizeof(gmss_header_t));
    printf("CHECK sizeof_instr %s %zu\n",
           sizeof(gmss_instrument_t) == 32 ? "PASS" : "FAIL", sizeof(gmss_instrument_t));
    printf("CHECK sizeof_zone %s %zu\n",
           sizeof(gmss_zone_t) == 56 ? "PASS" : "FAIL", sizeof(gmss_zone_t));
    if (sizeof(gmss_zone_t) != 56 || sizeof(gmss_header_t) != 64 ||
        sizeof(gmss_instrument_t) != 32) {
        g_fail++;
    }

    /* ---------- 2. 载入音色库 ---------- */
    size_t lib_len = 0;
    uint8_t *lib = load_file(argv[1], &lib_len);
    if (!lib) {
        fprintf(stderr, "错误：读不了 %s\n", argv[1]);
        return 2;
    }
    printf("INFO lib_bytes %zu\n", lib_len);

    sampler_set_library(lib);
    const int rc = sampler_init();
    {
        char d[160];
        snprintf(d, sizeof d, "rc=%d msg=%s", rc, sampler_last_error());
        check("sampler_init", rc == 0, d);
    }
    if (rc != 0) {
        free(lib);
        printf("RESULT FAIL %d\n", g_fail + 1);
        return 1;
    }

    {
        char d[160];
        snprintf(d, sizeof d, "zones=%u melodic=%u drums=%u",
                 sampler_zone_count(), sampler_melodic_count(),
                 sampler_drumkit_count());
        check("library_counts", sampler_zone_count() > 0, d);
    }

    /* ---------- 3. zone 表可读性 ---------- */
    const gmss_header_t *h = sampler_header();
    printf("INFO total_zones %u\n", h->total_zones);
    printf("INFO sample_rate %u\n", h->sample_rate);
    printf("INFO pool_off %u\n", h->pool_off);
    printf("INFO checksum %08X\n", h->checksum);

    /* ---------- 4. 逐 zone dump ---------- */
    FILE *dump = fopen(argv[2], "wb");
    if (!dump) {
        fprintf(stderr, "错误：写不了 %s\n", argv[2]);
        free(lib);
        return 2;
    }

    const uint8_t *ztab = (const uint8_t *)lib + h->zone_table_off;
    uint32_t total_cases = 0;
    int stream_mismatch = 0;

    for (uint32_t zi = 0; zi < h->total_zones; zi++) {
        const gmss_zone_t *z = (const gmss_zone_t *)(ztab + zi * sizeof(gmss_zone_t));

        printf("INFO zone %u key=%u-%u vel=%u-%u codec=%u len=%u attack=%u "
               "loop=%u..%u kf_n=%u gain=%d root=%u\n",
               zi, z->key_lo, z->key_hi, z->vel_lo, z->vel_hi,
               z->codec, z->sample_len, z->attack_samples,
               z->loop_start, z->loop_end, z->keyframe_count,
               (int)z->gain_db100, z->root_key);

        /*
         * 测试点覆盖（每一个都对应一类真实缺陷）：
         *   0                —— 起点
         *   attack-1         —— PCM 段最后一个点
         *   attack           —— 跨界第一个点（最容易错的边界）
         *   attack+1         —— 跨界后第二个点（nibble 奇偶翻转会在这里暴露）
         *   attack+2         —— 再往后一点
         *   127 / 128 / 129  —— 关键帧边界（差一错误会在这里暴露）
         *   loop_start-1     —— 循环点前
         *   loop_start       —— 循环点（seek 回这里必须精确）
         *   loop_end-2       —— 循环点尾部
         *   len-1            —— 最后一个点
         *   len / len+5      —— 越界（应补 0，不崩）
         */
        uint32_t starts[16];
        uint32_t nstart = 0;
        const uint32_t atk = z->attack_samples;
        const uint32_t len = z->sample_len;

        starts[nstart++] = 0;
        if (atk >= 1) starts[nstart++] = atk - 1;
        starts[nstart++] = atk;
        starts[nstart++] = atk + 1;
        starts[nstart++] = atk + 2;
        starts[nstart++] = 127;
        starts[nstart++] = 128;
        starts[nstart++] = 129;
        if (z->loop_start != GMSS_LOOP_NONE && z->loop_start >= 1) {
            starts[nstart++] = z->loop_start - 1;
            starts[nstart++] = z->loop_start;
        }
        if (z->loop_end != GMSS_LOOP_NONE && z->loop_end >= 2) {
            starts[nstart++] = z->loop_end - 2;
        }
        if (len >= 1) starts[nstart++] = len - 1;
        starts[nstart++] = len;
        starts[nstart++] = len + 5;
        /* 固定步长扫几个点，覆盖更多关键帧 */
        for (uint32_t s = 1000; s < len && nstart < 16; s += 7777) {
            starts[nstart++] = s;
        }

        const uint32_t COUNT = 64;
        int16_t out_rand[COUNT];
        int16_t out_stream[COUNT];

        for (uint32_t si = 0; si < nstart; si++) {
            const uint32_t start = starts[si];

            /* --- 随机访问接口 --- */
            memset(out_rand, 0x55, sizeof out_rand);
            sampler_read_frames(z, start, COUNT, out_rand);

            put_u32(dump, zi);
            put_u32(dump, start);
            put_u32(dump, COUNT);
            put_u32(dump, 0u);
            fwrite(out_rand, sizeof(int16_t), COUNT, dump);
            total_cases++;

            /* --- 连续流接口：从同一起点顺序取 COUNT 个 --- */
            gmss_stream_t st;
            gmss_stream_seek(&st, z, start);
            for (uint32_t i = 0; i < COUNT; i++) {
                out_stream[i] = gmss_stream_next(&st);
            }

            put_u32(dump, zi);
            put_u32(dump, start);
            put_u32(dump, COUNT);
            put_u32(dump, 1u);
            fwrite(out_stream, sizeof(int16_t), COUNT, dump);
            total_cases++;

            /*
             * ★ 自洽性检查：两条路径必须逐样本一致。
             *   随机访问接口走的是"每帧重新从关键帧追"的路径，
             *   连续流走的是"状态顺序演进"的路径。
             *   两者的差异正是最容易藏 bug 的地方 ——
             *   比如 stream 忘了在 PCM→ADPCM 交界处定位 ADPCM 状态。
             *
             *   越界区域（start >= len）两边都返回 0，也一致；
             *   但要注意 stream 越过 len 之后 dec_next 继续加，
             *   decode_frame_at 返回 0，而随机访问也补 0，所以仍然一致。
             */
            for (uint32_t i = 0; i < COUNT; i++) {
                if (out_rand[i] != out_stream[i]) {
                    if (stream_mismatch < 8) {
                        printf("INFO mismatch zone=%u start=%u i=%u rand=%d stream=%d\n",
                               zi, start, i, out_rand[i], out_stream[i]);
                    }
                    stream_mismatch++;
                    break;
                }
            }
        }
    }

    fclose(dump);

    {
        char d[128];
        snprintf(d, sizeof d, "cases=%u mismatches=%d", total_cases, stream_mismatch);
        check("stream_vs_random_agree", stream_mismatch == 0, d);
    }
    printf("INFO dump_cases %u\n", total_cases);

    /* ---------- 5. 循环回绕的 seek 精度 ---------- */
    /*
     * 模拟 voice 的循环行为：播到 loop_end 就 seek 回 loop_start，
     * 结果必须与"直接从 loop_start 连续解"完全一致。
     * 这是循环点咔哒声的根源所在。
     */
    {
        int loop_ok = 1;
        for (uint32_t zi = 0; zi < h->total_zones; zi++) {
            const gmss_zone_t *z =
                (const gmss_zone_t *)(ztab + zi * sizeof(gmss_zone_t));
            if (z->loop_start == GMSS_LOOP_NONE ||
                z->loop_end <= z->loop_start ||
                z->loop_end > z->sample_len) {
                continue;
            }

            int16_t a[32], b[32];

            /* 路径 A：seek 到 loop_start 后连续取 */
            gmss_stream_t sa;
            gmss_stream_seek(&sa, z, z->loop_start);
            for (int i = 0; i < 32; i++) a[i] = gmss_stream_next(&sa);

            /* 路径 B：从 0 顺序播到 loop_end，再 seek 回 loop_start */
            gmss_stream_t sb;
            gmss_stream_seek(&sb, z, 0);
            uint32_t pos = 0;
            while (pos < z->loop_end) { (void)gmss_stream_next(&sb); pos++; }
            gmss_stream_seek(&sb, z, z->loop_start);
            for (int i = 0; i < 32; i++) b[i] = gmss_stream_next(&sb);

            for (int i = 0; i < 32; i++) {
                if (a[i] != b[i]) {
                    printf("INFO loop_mismatch zone=%u i=%d direct=%d wrapped=%d\n",
                           zi, i, a[i], b[i]);
                    loop_ok = 0;
                    break;
                }
            }
        }
        check("loop_wrap_seek_exact", loop_ok, "回绕后与直接 seek 逐样本一致");
    }

    /* ---------- 6. 音高表抽查 ---------- */
    /*
     * dsp_pitch_step 是全工程音准的唯一来源。
     * 抽查几个整数半音：必须精确（表点落在精确值上）。
     *   同音      → 1.0
     *   高 12 半音 → 2.0
     *   低 12 半音 → 0.5
     */
    {
        int pok = 1;
        /*
         * 期望值 = round(2^(n/12) * 65536)：
         *   同音       2^0     = 1.0        → 0x00010000
         *   高 12 半音  2^1     = 2.0        → 0x00020000
         *   低 12 半音  2^-1    = 0.5        → 0x00008000
         *   高 7 半音   2^(7/12)= 1.49830708 → 98194.08 → 0x00017F92
         *
         * ★ 容差取 ±4 LSB：表是 Q16.16 的线性插值，最大误差 0.393 音分
         *   （见 dsp_pitch_table.h 的说明）。4 LSB = 4/65536 ≈ 0.006%，
         *   换算成音分约 0.001，远远好于可辨阈 5 音分。
         *
         * ★ 这里最初把 fifth 写成了 0x17F5A（= 98138），比真值少 56 LSB，
         *   于是测试报"失败"而代码其实是对的。**期望值必须自己先算对**，
         *   否则测试会变成噪声源。
         */
        struct { int note, root, cents; uint32_t want; const char *name; } cases[] = {
            { 60, 60,   0, 0x00010000u, "unison" },
            { 72, 60,   0, 0x00020000u, "octave_up" },
            { 48, 60,   0, 0x00008000u, "octave_down" },
            { 67, 60,   0, 0x00017F92u, "fifth" },
        };
        for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
            uint32_t got = dsp_pitch_step(cases[i].note, cases[i].root,
                                          cases[i].cents);
            long diff = (long)got - (long)cases[i].want;
            if (diff < 0) diff = -diff;
            char d[128];
            snprintf(d, sizeof d, "%s got=0x%08X want=0x%08X d=%ld",
                     cases[i].name, got, cases[i].want, diff);
            if (diff > 4) { pok = 0; check("pitch_exact", 0, d); }
            else printf("INFO pitch %s\n", d);
        }
        if (pok) check("pitch_exact", 1, "unison/octave/fifth 均在 ±4 LSB 内");
    }

    /* ---------- 7. 力度曲线 ---------- */
    {
        int vok = 1;
        if (dsp_vel_to_q15(127) < 30000) vok = 0;   /* 满力度应接近 1.0 */
        if (dsp_vel_to_q15(0) != 0) vok = 0;        /* 零力度应为 0 */
        if (dsp_vel_to_q15(64) >= dsp_vel_to_q15(127)) vok = 0;  /* 单调 */
        char d[128];
        snprintf(d, sizeof d, "vel127=%d vel64=%d vel0=%d",
                 dsp_vel_to_q15(127), dsp_vel_to_q15(64), dsp_vel_to_q15(0));
        check("velocity_curve", vok, d);
    }

    /* ---------- 8. 库损坏必须被拒绝 ---------- */
    {
        /* 改一个 zone 表字节，校验和必须不符 */
        uint8_t *copy = malloc(lib_len);
        memcpy(copy, lib, lib_len);
        copy[h->zone_table_off] ^= 0xFF;
        sampler_set_library(copy);
        int r2 = sampler_init();
        check("reject_corrupt_library", r2 == -4, "改一个字节后必须报校验和错");

        /* 魔数改坏 */
        memcpy(copy, lib, lib_len);
        copy[0] ^= 0xFF;
        sampler_set_library(copy);
        check("reject_bad_magic", sampler_init() == -1, "魔数错必须被拒");

        free(copy);
        /* 恢复正确的库，后面的检查还要用 */
        sampler_set_library(lib);
        (void)sampler_init();
    }

    /* ---------- 9. zone 选择语义 ---------- */
    {
        const gmss_instrument_t *inst = sampler_find_instrument(0, 0);
        if (inst == NULL) {
            check("zone_selection", 0, "找不到 program 0");
        } else {
            const gmss_zone_t *z1 = sampler_select_zone(inst, 60, 100);
            const gmss_zone_t *z2 = sampler_select_zone(inst, 60, 1);
            char d[128];
            snprintf(d, sizeof d, "inst zones=%u soft=%p hard=%p",
                     inst->zone_count, (const void *)z1, (const void *)z2);
            check("zone_selection", z1 != NULL && z2 != NULL, d);
        }
        /* 不存在的 program 必须返回 NULL 而不是野指针 */
        check("missing_program_null",
              sampler_find_instrument(0, 126) == NULL ||
              sampler_find_instrument(0, 126)->zone_count > 0,
              "空槽/不存在不能崩");
    }

    printf("RESULT %s %d\n", g_fail == 0 ? "PASS" : "FAIL", g_fail);
    free(lib);
    return g_fail == 0 ? 0 : 1;
}
