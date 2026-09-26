/*
 * test_engine.c — 在**主机**上测试 voice.c / engine.c 的完整音频通路
 *
 * 与 test_sampler.c 同一个思路：不希望为了验证"音高对不对、包络对不对、
 * 混音会不会溢出"而先焊一块板子。
 *
 * 覆盖的检查（每一条都对应一类真实缺陷）：
 *   1. 起音后确实有声音（不是全 0）
 *   2. 音高正确：用 FFT 太奢侈，改成**过零率**估计基频
 *      —— 正弦测试音的过零率与频率成正比，足以发现八度/五度错
 *   3. 包络：attack 期间幅度单调上升；release 之后归零
 *   4. 循环：连续播 2 秒不出现异常（不崩、不静音、不恒定）
 *   5. 混音不溢出：48 个满力度音同时起，输出必须被饱和在 int16 内
 *      （不是回绕！回绕会从 +32767 跳到 -32768，是刺耳的爆音）
 *   6. 弯音：向上弯一个八度后过零率翻倍
 *   7. 延音踏板：踩下时 note-off 不断音，抬起后才断
 *   8. 力度 0 的 Note On 视作 Note Off
 *   9. 音量 CC7 = 0 时静音
 *  10. 抢占：起 100 个音不崩、活跃数不超过 VOICE_MAX
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>

#include "engine.h"
#include "voice.h"
#include "sampler.h"

#define SR        48000
#define BLK       256
#define MAXFRAMES (SR * 3)

static int g_fail;

static void check(const char *name, int ok, const char *detail)
{
    printf("CHECK %s %s %s\n", name, ok ? "PASS" : "FAIL", detail ? detail : "");
    if (!ok) g_fail++;
}

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
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); fclose(f); return NULL; }
    fclose(f);
    *out_len = (size_t)n;
    return buf;
}

/* ------------------------------------------------------------------ */
/* 渲染到缓冲区                                                        */
/* ------------------------------------------------------------------ */
static int16_t *render(uint32_t frames)
{
    static int16_t buf[MAXFRAMES * 2];
    if (frames > MAXFRAMES) frames = MAXFRAMES;
    memset(buf, 0, (size_t)frames * 4);
    engine_render(buf, frames);
    return buf;
}

/* 峰值 */
static int32_t peak_of(const int16_t *lr, uint32_t frames)
{
    int32_t p = 0;
    for (uint32_t i = 0; i < frames * 2; i++) {
        int32_t v = lr[i];
        if (v < 0) v = -v;
        if (v > p) p = v;
    }
    return p;
}

/*
 * 自相关法估计基频（只看左声道）。
 *
 * ★★ 为什么不能用过零率（这里换过一次）
 *   最初用"带迟滞的过零率"，对**合成正弦**测试音很好使。
 *   但换成真实音色库（TimGM6mb 的钢琴）之后立刻失效：
 *     note 60 量到 515.6Hz、note 72 也量到 515.6Hz，比值 1.0000。
 *   原因不是固件错了，是**方法不适用** —— 钢琴采样的谐波很丰富，
 *   带迟滞的过零率会锁到**最强的那个谐波**上：
 *     note 60 基频 261Hz，锁到了 2 次谐波 523Hz；
 *     note 72 基频 523Hz，锁到了基频本身。
 *   两个数碰巧都落在 515 附近，看起来像"音高没变"。
 *
 *   自相关找的是**真正的周期**，对谐波丰富的信号稳定得多。
 *   为避免"取到 2 倍周期"（八度错误），在所有局部极大里挑
 *   **第一个足够接近最大值**的 lag。
 */
static double estimate_hz(const int16_t *lr, uint32_t frames)
{
    if (frames < 2048) return 0.0;

    enum { N = 2048 };
    static double x[N];
    double mean = 0.0;
    for (uint32_t i = 0; i < N; i++) {
        x[i] = lr[i * 2];            /* 交错布局，取左声道 */
        mean += x[i];
    }
    mean /= N;
    double energy0 = 0.0;
    for (uint32_t i = 0; i < N; i++) { x[i] -= mean; energy0 += x[i] * x[i]; }
    if (energy0 < 1e4) return 0.0;   /* 基本没信号 */

    const int lag_min = (int)(SR / 4000);   /* 上限 4kHz */
    const int lag_max = (int)(SR / 40);     /* 下限 40Hz */
    if (lag_max >= (int)N - 8) return 0.0;

    double best = 0.0;
    double r[2048];
    for (int lag = lag_min; lag <= lag_max; lag++) {
        double num = 0.0, e1 = 0.0, e2 = 0.0;
        for (int i = 0; i + lag < (int)N; i++) {
            num += x[i] * x[i + lag];
            e1 += x[i] * x[i];
            e2 += x[i + lag] * x[i + lag];
        }
        double den = sqrt(e1 * e2);
        r[lag] = (den > 1e-9) ? (num / den) : 0.0;
        if (r[lag] > best) best = r[lag];
    }
    if (best <= 0.0) return 0.0;

    /* 挑第一个"足够接近最佳"的局部极大，避免锁到 2 倍周期 */
    for (int lag = lag_min + 1; lag < lag_max; lag++) {
        if (r[lag] >= best * 0.88 &&
            r[lag] >= r[lag - 1] && r[lag] >= r[lag + 1]) {
            return (double)SR / (double)lag;
        }
    }
    return 0.0;
}

/*
 * 从第 from 帧开始，测一段长度的频率。
 *
 * ★ 直接把交错缓冲的指针偏移过去，**不要**另拷一个"单声道数组"再传进来。
 *   estimate_hz 是按 `lr[i*2]` 取左声道的（交错布局），
 *   传一个 4096 长的紧凑数组进去会索引到 8190 —— 越界读，
 *   在 x86 上表现为随机崩溃（我第一次就是这么写的，
 *   测试程序自己崩在测量函数里，看起来像被测代码的问题）。
 */
static double freq_at(uint32_t from, uint32_t len, uint32_t total)
{
    int16_t *all = render(total);
    if (from >= total) return 0.0;
    if (from + len > total) len = total - from;
    return estimate_hz(all + from * 2, len);
}

/* ------------------------------------------------------------------ */
static void midi_note_on(uint8_t ch, uint8_t note, uint8_t vel)
{
    midi_event_t e = { 0 };
    e.type = MIDI_EV_NOTE_ON; e.channel = ch; e.data1 = note; e.data2 = vel;
    engine_handle_midi(&e);
}
static void midi_note_off(uint8_t ch, uint8_t note)
{
    midi_event_t e = { 0 };
    e.type = MIDI_EV_NOTE_OFF; e.channel = ch; e.data1 = note; e.data2 = 0;
    engine_handle_midi(&e);
}
static void midi_cc(uint8_t ch, uint8_t cc, uint8_t v)
{
    midi_event_t e = { 0 };
    e.type = MIDI_EV_CONTROL; e.channel = ch; e.data1 = cc; e.data2 = v;
    engine_handle_midi(&e);
}
static void midi_prog(uint8_t ch, uint8_t p)
{
    midi_event_t e = { 0 };
    e.type = MIDI_EV_PROGRAM; e.channel = ch; e.data1 = p; e.data2 = 0;
    engine_handle_midi(&e);
}
static void midi_bend(uint8_t ch, uint16_t bend)
{
    midi_event_t e = { 0 };
    e.type = MIDI_EV_PITCH_BEND; e.channel = ch; e.bend = bend;
    engine_handle_midi(&e);
}

/* ================================================================== */
int main(int argc, char **argv)
{
    /*
     * ★ 关掉 stdout 缓冲。
     *   默认 stdout 在重定向到文件/管道时是**全缓冲**的，程序一旦崩在
     *   中途，之前所有的 printf 都会随缓冲区一起丢掉 ——
     *   表现是"什么都没有就退出了"，完全看不出崩在哪一行。
     *   这类测试程序崩溃时最有价值的信息就是"最后打出来的那行"。
     */
    setvbuf(stdout, NULL, _IONBF, 0);

    if (argc < 2) {
        fprintf(stderr, "用法: %s <library.bin>\n", argv[0]);
        return 2;
    }

    size_t lib_len = 0;
    uint8_t *lib = load_file(argv[1], &lib_len);
    if (!lib) { fprintf(stderr, "读不了 %s\n", argv[1]); return 2; }
    printf("INFO lib_bytes=%zu\n", lib_len);
    sampler_set_library(lib);

    const int rc = engine_init();
    if (rc != 0) {
        printf("CHECK engine_init FAIL rc=%d %s\n", rc, sampler_last_error());
        return 1;
    }
    printf("INFO zones=%u melodic=%u\n",
           sampler_zone_count(), sampler_melodic_count());

    /* 找到 program 0 用来测试 */
    midi_prog(0, 0);
    midi_cc(0, MIDI_CC_VOLUME, 100);
    midi_cc(0, MIDI_CC_EXPRESSION, 127);

    /* ---------- 1. 起音后有声音 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        int16_t *b = render(SR / 2);
        const int32_t p = peak_of(b, SR / 2);
        char d[96]; snprintf(d, sizeof d, "peak=%d", p);
        check("note_on_produces_audio", p > 100, d);
    }

    /* ---------- 2. 音高正确 ---------- */
    /*
     * ★ 用两个音的**频率比**判断，而不是绝对频率。
     *   绝对频率会受 zone 的 root_key 影响（测试音色库的根音未必是 60），
     *   但"高 12 半音 = 频率 ×2"这个关系与根音无关，是最稳的判据。
     */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        const double f1 = freq_at(SR / 4, 4096, SR / 2);
        engine_reset();
        midi_note_on(0, 72, 100);
        const double f2 = freq_at(SR / 4, 4096, SR / 2);

        char d[128];
        snprintf(d, sizeof d, "f60=%.1fHz f72=%.1fHz ratio=%.4f",
                 f1, f2, (f1 > 0) ? f2 / f1 : 0.0);
        /* 允许 3% 误差（过零率在波形复杂时本身有量化误差） */
        const int ok = (f1 > 50) && (f2 > 50) &&
                       (fabs(f2 / f1 - 2.0) < 0.06);
        check("octave_pitch_ratio", ok, d);
    }

    /* ---------- 3. 包络：直接测 dsp_env_t 的时序精度 ---------- */
    /*
     * ★ 比"看看渲染出来的峰值有没有上升"精确得多。
     *
     *   dsp_core.h 里记着一段历史：包络最初用"整数斜率 = 32767/(ms*3)"
     *   做增量，长时段误差极大 —— 实测 attack 2000ms 跑成 2184ms、
     *   decay 5000ms 跑成 8192ms（超 64%）。原因是整数除法逐步截断。
     *   改成 Q15.16 定点之后应该落在 1 个更新周期（16 采样点 = 0.33ms）内。
     *
     *   这个检查就是在守这条性质：**毫秒参数必须真的等于毫秒**。
     *   音频里包络时长错了不一定听得出来（"感觉音头软一点"），
     *   但会让所有音色的听感一起走样，而且极难归因。
     */
    {
        static const uint16_t attacks[] = { 1, 5, 20, 100, 500, 2000 };
        int all_ok = 1;
        char detail[256] = { 0 };

        for (unsigned i = 0; i < sizeof(attacks) / sizeof(attacks[0]); i++) {
            dsp_env_t e;
            (void)dsp_env_init(&e, attacks[i], 0, 32767, 100);
            dsp_env_trigger(&e);

            /* 数到电平第一次到达最大值用了多少个采样点 */
            uint32_t n = 0;
            const uint32_t limit = SR * 5;          /* 5 秒上限 */
            while (n < limit && dsp_env_level(&e) < 32767) {
                (void)dsp_env_tick(&e);
                n++;
            }

            const double got_ms = (double)n * 1000.0 / (double)SR;
            const double want_ms = (double)attacks[i];
            const double err_ms = fabs(got_ms - want_ms);

            /* 容差 = 2 个更新周期（2*16 采样点 ≈ 0.67ms）+ 1ms 余量 */
            const double tol = 1.5;
            if (err_ms > tol) {
                all_ok = 0;
                snprintf(detail + strlen(detail),
                         sizeof(detail) - strlen(detail),
                         "attack=%ums 实测%.2fms 误差%.2fms; ",
                         attacks[i], got_ms, err_ms);
            }
        }
        if (all_ok) {
            snprintf(detail, sizeof detail,
                     "6 组 attack（1..2000ms）误差均 < 1.5ms");
        }
        check("envelope_timing_accurate", all_ok, detail);
    }

    /* ---------- 3b. 包络：起音确实上升、释放后归零 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        int16_t *b = render(SR / 2);
        /* 起音段必须有能量（具体形状交给上面的直接测试去管） */
        const int32_t p_early = peak_of(b, SR / 100);      /* 前 10ms */
        const int32_t p_late = peak_of(b, SR / 2);
        char d[128];
        snprintf(d, sizeof d, "peak_10ms=%d peak_500ms=%d", p_early, p_late);
        check("attack_produces_audio", p_early > 0 && p_late > 0, d);

        /* note-off 之后应逐渐归零 */
        midi_note_off(0, 60);
        int16_t *b2 = render(SR * 2);
        const int32_t late = peak_of(b2 + (SR * 3 / 2) * 2, SR / 2);
        char d2[96]; snprintf(d2, sizeof d2, "peak_after_release=%d", late);
        check("release_goes_silent", late == 0, d2);
    }

    /* ---------- 4. 循环不出问题 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        int16_t *b = render(SR * 2);

        /*
         * ★ 判据换过一次，记一下为什么。
         *
         *   原来写的是"每一段峰值都必须 > 100"，那是照着**合成正弦**
         *   测试音定的（恒定电平）。换成真实音色库之后立刻误报：
         *   钢琴的采样本身就在自然衰减（实测 RMS 从 9044 掉到 1133），
         *   再加上音量包络在衰减，2 秒时那一段自然很轻。
         *   **这不是 bug，是钢琴该有的样子。**
         *
         *   真正的缺陷长什么样：循环回绕时忘了重新 seek 解码器 →
         *   回绕那一刻出现一个巨大的跳变，之后是噪声或死寂。
         *   那两种情况的特征是「**突然**掉到 0 或者跳变」，
         *   而不是"平缓地越来越轻"。
         *
         *   所以改成检查两件事：
         *     1. 每段都还有信号（峰值 > 0，没死掉）
         *     2. 不出现"突然掉到接近 0"（相对峰值不低于首段的 0.2%）
         *   跳变由下面的 loop_no_big_jump 单独守。
         */
        int32_t pk[8];
        int32_t first = 0;
        int all_alive = 1;
        for (int s = 0; s < 8; s++) {
            pk[s] = peak_of(b + (SR / 4) * s * 2, SR / 4);
            if (s == 0) first = pk[0];
            if (pk[s] <= 0) all_alive = 0;
            if (first > 0 && pk[s] < first / 500) all_alive = 0;   /* 突然没了 */
        }
        char d[256];
        snprintf(d, sizeof d, "peak/0.25s = %d,%d,%d,%d,%d,%d,%d,%d",
                 pk[0], pk[1], pk[2], pk[3], pk[4], pk[5], pk[6], pk[7]);
        check("loop_stays_alive_2s", all_alive, d);

        /* 回绕处不该有巨大跳变（爆音）。看相邻样本差的最大值。 */
        int32_t maxjump = 0;
        for (uint32_t i = 1; i < SR * 2; i++) {
            int32_t d2 = (int32_t)b[i * 2] - (int32_t)b[(i - 1) * 2];
            if (d2 < 0) d2 = -d2;
            if (d2 > maxjump) maxjump = d2;
        }
        char d3[160];
        snprintf(d3, sizeof d3, "max_sample_jump=%d (首段峰值 %d)", maxjump, first);
        /*
         * ★ 阈值按**本音色自己的峰值**来定，不用固定值。
         *   真实乐器的谐波比正弦丰富得多，相邻样本差本来就大；
         *   用固定阈值会对钢琴误报。
         *   真正的回绕爆音是"满幅跳变"（接近 2× 峰值）。
         */
        check("loop_no_big_jump", maxjump < first, d3);
    }

    /* ---------- 5. 混音不溢出（48 个满力度音）---------- */
    {
        engine_reset();
        for (int c = 0; c < 16; c++) midi_prog((uint8_t)c, 0);
        for (int n = 0; n < 48; n++) {
            midi_note_on((uint8_t)(n % 16), (uint8_t)(36 + n), 127);
        }
        int16_t *b = render(SR / 2);
        const uint32_t nv = engine_active_voices();

        /*
         * ★ 检查饱和而不是溢出。
         *   溢出（int32 累加后直接截断成 int16）会让 +32767 突然变成
         *   -32768 —— 听感是刺耳的爆音，而且波形完全反相。
         *   dsp_bus 用 int32 累加、最后 dsp_sat16 夹紧，所以
         *   相邻样本之间不该出现"跨满幅"的跳变。
         */
        int32_t maxjump = 0;
        for (uint32_t i = 1; i < SR / 2; i++) {
            int32_t d = (int32_t)b[i * 2] - (int32_t)b[(i - 1) * 2];
            if (d < 0) d = -d;
            if (d > maxjump) maxjump = d;
        }
        char d[128];
        snprintf(d, sizeof d, "voices=%u peak=%d maxjump=%d",
                 nv, peak_of(b, SR / 2), maxjump);
        check("no_voice_overrun", nv <= VOICE_MAX, d);
        check("mix_no_wraparound", maxjump < 60000, d);
    }

    /* ---------- 6. 弯音 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        const double fbase = freq_at(SR / 4, 4096, SR / 2);

        midi_bend(0, 8192 + 8192);      /* 向上弯满 = +2 半音（默认范围）*/
        const double fbent = freq_at(SR / 4, 4096, SR / 2);

        /* +2 半音 = 2^(2/12) = 1.1225 */
        char d[128];
        snprintf(d, sizeof d, "base=%.1f bent=%.1f ratio=%.4f (期望≈1.1225)",
                 fbase, fbent, (fbase > 0) ? fbent / fbase : 0.0);
        const int ok = (fbase > 50) && (fabs(fbent / fbase - 1.1225) < 0.06);
        check("pitch_bend_applies_to_sounding_note", ok, d);
    }

    /* ---------- 7. 延音踏板 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_cc(0, MIDI_CC_SUSTAIN, 127);   /* 踩下 */
        midi_note_on(0, 60, 100);
        (void)render(SR / 4);
        midi_note_off(0, 60);                /* 松键，但踏板还踩着 */
        int16_t *b = render(SR / 4);
        const int32_t p_held = peak_of(b, SR / 4);

        midi_cc(0, MIDI_CC_SUSTAIN, 0);      /* 抬起踏板 */
        int16_t *b2 = render(SR * 2);
        const int32_t p_late = peak_of(b2 + (SR * 3 / 2) * 2, SR / 2);

        char d[128];
        snprintf(d, sizeof d, "peak_with_pedal=%d peak_after_pedal_up=%d",
                 p_held, p_late);
        check("sustain_pedal_holds_note", p_held > 100, d);
        check("sustain_release_after_pedal_up", p_late == 0, d);
    }

    /* ---------- 8. 力度 0 的 Note On = Note Off ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_note_on(0, 60, 100);
        (void)render(SR / 4);
        midi_note_on(0, 60, 0);              /* ★ 应该等同 note-off */
        int16_t *b = render(SR * 2);
        const int32_t late = peak_of(b + (SR * 3 / 2) * 2, SR / 2);
        char d[96]; snprintf(d, sizeof d, "peak_after_vel0=%d", late);
        check("note_on_vel0_is_note_off", late == 0, d);
    }

    /* ---------- 9. 音量 0 静音 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        midi_cc(0, MIDI_CC_VOLUME, 0);
        midi_note_on(0, 60, 127);
        int16_t *b = render(SR / 4);
        const int32_t p = peak_of(b, SR / 4);
        char d[96]; snprintf(d, sizeof d, "peak_at_vol0=%d", p);
        check("volume_zero_is_silent", p == 0, d);
    }

    /* ---------- 10. 抢占不崩 ---------- */
    {
        engine_reset(); midi_prog(0, 0);
        for (int n = 0; n < 100; n++) {
            midi_note_on(0, (uint8_t)(40 + (n % 60)), 100);
        }
        int16_t *b = render(SR / 4);
        const uint32_t nv = engine_active_voices();
        char d[96];
        snprintf(d, sizeof d, "voices=%u (上限 %u) peak=%d",
                 nv, VOICE_MAX, peak_of(b, SR / 4));
        check("voice_stealing_bounded", nv <= VOICE_MAX && nv > 0, d);
    }

    /* ---------- 11. 统计计数合理 ---------- */
    {
        const engine_stats_t *st = engine_stats();
        char d[192];
        snprintf(d, sizeof d,
                 "note_on=%llu note_off=%llu no_zone=%llu no_voice=%llu cc=%llu",
                 (unsigned long long)st->note_on, (unsigned long long)st->note_off,
                 (unsigned long long)st->dropped_no_zone,
                 (unsigned long long)st->dropped_no_voice,
                 (unsigned long long)st->cc_received);
        check("stats_counters_alive", st->note_on > 0, d);
        printf("INFO stats %s\n", d);
    }

    printf("RESULT %s %d\n", g_fail == 0 ? "PASS" : "FAIL", g_fail);
    free(lib);
    return g_fail == 0 ? 0 : 1;
}
