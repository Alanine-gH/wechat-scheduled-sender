"""微信定时群发控制台 · 宣传片生成脚本（竖屏 1080x1920 / 30fps / 约 60 秒）

沿用 dsh-huaweicloud-connect 宣传视频的流水线：
  1. PIL 逐场景绘制 1080x1920 画面（深色科技风，蓝色主调）
  2. edge-tts 生成中文配音（zh-CN-YunxiNeural，+12%）
  3. ffmpeg 把每条配音变速对齐场景时长
  4. 生成 ASS / SRT 硬字幕（按分句长度分配时间）
  5. 每个场景渲染成 clip：Ken Burns 缓慢放大 + 首尾淡入淡出
  6. concat 拼接
  7. numpy 合成原创电子节拍 BGM（不引用任何外部音乐）
  8. 混音 + 烧字幕输出成片

依赖：ffmpeg / ffprobe、edge-tts、numpy、Pillow
运行：cd 工程 && python 制作视频.py
"""
import asyncio
import json
import os
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import edge_tts

ROOT = Path(__file__).resolve().parents[1]      # 宣传视频/
BASE = ROOT.parent                               # 项目根（放封面等素材）
WORK = ROOT / '工程'
W, H = 1080, 1920
FONT = 'C:/Windows/Fonts/msyh.ttc'
BOLD = 'C:/Windows/Fonts/msyhbd.ttc'
MONO = 'C:/Windows/Fonts/consola.ttf'

REPO = 'github.com/Alanine-gH/wechat-scheduled-sender'

# (时长秒, 标题, 英文主视觉, 副标题, 配音)
scenes = [
    (5, '几十个群发通知\n一个个复制粘贴', 'MANUAL → AUTO', '手酸之前，先看看这个',
     '给几十个群发通知，一个个复制粘贴，太累了。这个工具能自动发。'),
    (5, '一个开源工具\n替你按点发群', 'WECHAT SCHEDULER', 'Electron 桌面应用 · 完全本地运行',
     '它叫微信定时群发控制台，完全本地运行。'),
    (7, '不靠鼠标坐标\n窗口怎么动都不怕', 'UIA', '走无障碍控件 · 不是模拟点击',
     '关键在这：它通过无障碍控件驱动微信，不是模拟鼠标点击，窗口移动缩放都不影响。'),
    (6, '账号、群、好友\n自动列出来', 'AUTO LIST', '群聊与好友分组 · 直接多选',
     '自动列出本机微信账号，以及每个账号的群聊和好友，分开勾选。'),
    (6, '内容写进\n一个文本文件', 'MESSAGES.TXT', '每行一条 · 多行消息用 --- 分隔',
     '要发的内容放在一个文本文件里，一行一条，也能写带换行的整段。'),
    (6, '按日期随机\n间隔约五分钟', 'SEED = DATE', '同一天的计划完全可复现',
     '发送间隔用当天日期做随机种子，平均五分钟一条，同一天完全可复现。'),
    (6, '每发一条\n都留证据', 'PROOF', '自动截图存档 · 数据库回读校验',
     '每发一条都自动截图存档，并回读数据库确认真的发出去了。'),
    (6, '用户文件\n只在安装目录', 'NO HOME DIR', '不写用户目录 · 不写临时目录',
     '所有用户文件都放在安装目录下，不往系统用户目录写东西。'),
    (13, '开源给你用\n先看三条提醒', 'OPEN SOURCE', REPO,
     '源码在仓库里，MIT 协议。提醒三点：客户端界面自动化，不是官方接口；高频群发有风控风险；内容请合法合规，别发垃圾信息。自用就好。'),
]

# 蓝色主调（白为主、黑为辅、蓝为辅色；风险提示用特殊色）
C_BG_TOP = (10, 18, 32)
C_BG_BOT = (22, 40, 63)
C_GRID = (24, 45, 64)
C_TEXT = '#F4F7FC'
C_MUTED = '#9FB5C9'
C_DIM = '#6F91A6'
C_BLUE = '#5AA9FF'
C_BLUE_SOFT = '#8FD0FF'
C_CARD = '#14263B'
C_CARD_HL = '#1E3D5C'
C_CARD_WARN = '#3A2436'
C_WARN = '#FF9B6A'


def run(args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *map(str, args)],
                   check=True)


def probe(p):
    return float(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', str(p)]))


def font(s, b=False):
    return ImageFont.truetype(BOLD if b else FONT, s)


def txt(d, xy, t, s=42, c=C_TEXT, b=False):
    d.text(xy, t, font=font(s, b), fill=c, spacing=18)


def wrap(t, n=18):
    return '\n'.join(t[i:i + n] for i in range(0, len(t), n))


def card(d, box, fill=C_CARD, outline='#304C65'):
    d.rounded_rectangle(box, 28, fill=fill, outline=outline, width=2)


def draw_scene(i, sc):
    duration, title, hero, sub, voice = sc
    a = np.zeros((H, W, 3), dtype=np.uint8)
    for y in range(H):
        k = y / H
        a[y, :, 0] = C_BG_TOP[0] + int(k * (C_BG_BOT[0] - C_BG_TOP[0]))
        a[y, :, 1] = C_BG_TOP[1] + int(k * (C_BG_BOT[1] - C_BG_TOP[1]))
        a[y, :, 2] = C_BG_TOP[2] + int(k * (C_BG_BOT[2] - C_BG_TOP[2]))
    im = Image.fromarray(a)
    d = ImageDraw.Draw(im)

    # 网格底纹
    for x in range(0, W, 90):
        d.line((x, 0, x, H), fill=C_GRID)
    for y in range(0, H, 90):
        d.line((0, y, W, y), fill=C_GRID)

    # 顶部标签
    d.rounded_rectangle((70, 160, 560, 215), 27, fill='#18354D')
    txt(d, (94, 168), 'WECHAT  /  SCHEDULER', 26, C_BLUE_SOFT, True)

    # 标题与副标题
    txt(d, (70, 290), title, 74, b=True)
    txt(d, (74, 535), sub, 32, C_MUTED)

    if i == 0:
        txt(d, (75, 650), 'AUTO', 200, C_BLUE, True)
        txt(d, (80, 900), '不再', 86, C_BLUE_SOFT, True)
        txt(d, (80, 1015), '一个个点', 100, C_BLUE_SOFT, True)
        card(d, (70, 1240, 1010, 1400))
        txt(d, (110, 1290), '把重复劳动交给程序', 56, b=True)
    elif i == 1:
        for y, t, hl in [(670, 'Electron 桌面应用', False),
                         (960, 'wechat-scheduled-sender', True),
                         (1250, 'Python 自动化引擎', False)]:
            card(d, (70, y, 1010, y + 140), fill=C_CARD_HL if hl else C_CARD)
            txt(d, (110, y + 38), t, 46 if hl else 49,
                C_BLUE if hl else C_TEXT, True)
        txt(d, (495, 815), '↓', 85, C_BLUE)
        txt(d, (495, 1105), '↓', 85, C_BLUE)
        txt(d, (80, 1450), '一个窗口，管完所有群', 28, C_MUTED)
    elif i == 2:
        txt(d, (70, 660), '鼠标坐标', 96, C_DIM, True)
        card(d, (70, 800, 1010, 900), fill=C_CARD_WARN)
        txt(d, (110, 828), '不用它', 62, C_WARN, True)
        for y, t in [(1020, '按无障碍控件名定位会话与输入框'),
                     (1190, '不依赖剪贴板，不依赖窗口位置'),
                     (1360, '窗口移动、缩放、被遮挡都不影响')]:
            card(d, (70, y, 1010, y + 130))
            txt(d, (110, y + 42), t, 42, b=True)
    elif i == 3:
        for y, t in [(650, '本机账号 → 自动枚举'),
                     (830, '群聊 / 好友 → 分开显示'),
                     (1010, '勾选目标 → 支持多选')]:
            card(d, (70, y, 1010, y + 150))
            txt(d, (110, y + 48), t, 46, b=True)
        card(d, (70, 1230, 1010, 1430), fill='#0C1A28')
        txt(d, (110, 1265), '群聊 29  ·  好友 7', 52, C_BLUE, True)
        txt(d, (110, 1345), '示例数量，实际以你的账号为准', 30, C_MUTED)
    elif i == 4:
        card(d, (70, 650, 1010, 1120), fill='#0C1A28')
        txt(d, (110, 690), '第一行内容', 40, C_MUTED)
        txt(d, (110, 755), '第二行内容', 40, C_MUTED)
        txt(d, (110, 820), '第三行内容', 40, C_MUTED)
        txt(d, (110, 900), '---', 46, C_BLUE, True)
        txt(d, (110, 975), '另一条消息', 40, C_MUTED)
        txt(d, (110, 1052), 'messages.txt', 30, C_DIM)
        for y, t in [(1170, '每行一条 ／ 也可整段多行'),
                     (1340, '发送时从池中随机抽取')]:
            card(d, (70, y, 1010, y + 130))
            txt(d, (110, y + 42), t, 42, b=True)
    elif i == 5:
        card(d, (70, 650, 1010, 1000), fill='#0C1A28')
        txt(d, (110, 700), 'random.seed("2026-11-02")', 40, C_BLUE, True)
        txt(d, (110, 780), '间隔随机 · 当天可复现', 36, C_MUTED)
        txt(d, (110, 872), '30 个群 × 约 5 分钟', 46, C_TEXT, True)
        txt(d, (110, 938), '≈ 2.5 小时发完', 46, C_BLUE, True)
        for y, t in [(1080, '每条之间独立等待，不扎堆'),
                     (1250, '轮数 / 条数 / 间隔都可在界面调')]:
            card(d, (70, y, 1010, y + 130))
            txt(d, (110, y + 42), t, 42, b=True)
    elif i == 6:
        for y, t in [(660, '发送前自动截图 → shots/日期/'),
                     (840, '发送后回读数据库 → 确认真的发出'),
                     (1020, '日志标记「已校验」才算成功')]:
            card(d, (70, y, 1010, y + 140))
            txt(d, (110, y + 45), t, 42, b=True)
        card(d, (70, 1220, 1010, 1370), fill=C_CARD_HL)
        txt(d, (110, 1262), '不是「点了就算发了」', 48, C_BLUE, True)
    elif i == 7:
        txt(d, (70, 650), 'C:\\Users\\...', 78, C_DIM, True)
        card(d, (70, 800, 1010, 900), fill=C_CARD_WARN)
        txt(d, (110, 828), '不写这里', 62, C_WARN, True)
        card(d, (70, 1000, 1010, 1160), fill=C_CARD_HL)
        txt(d, (110, 1035), 'data / logs / shots / cache', 44, C_BLUE, True)
        txt(d, (110, 1260), '全部在安装目录下', 56, b=True)
        for y, t in [(1380, '数据库密钥、聊天截图都不外传')]:
            card(d, (70, y, 1010, y + 130))
            txt(d, (110, y + 42), t, 40, b=True)
    else:
        for y, t, warn in [(620, '01  客户端界面自动化 · 非官方接口', True),
                           (770, '02  高频群发有账号风控风险', True),
                           (920, '03  内容须合法合规 · 已获群成员同意', True)]:
            card(d, (70, y, 1010, y + 122), fill=C_CARD_WARN)
            txt(d, (100, y + 38), t, 38, C_WARN, True)
        txt(d, (70, 1100), '开源 · 自用就好', 62, C_BLUE, True)
        txt(d, (74, 1210), REPO, 36, C_TEXT)
        txt(d, (74, 1290), 'MIT License', 34, C_MUTED)

    # 页脚：仓库名 + 进度
    txt(d, (75, 1640), 'wechat-scheduled-sender', 35, C_BLUE, True)
    txt(d, (75, 1770), 'ALANINE  /  OPEN SOURCE TOOL', 24, C_DIM)
    txt(d, (920, 1770), f'{i + 1:02d}/09', 24, C_DIM)
    d.rectangle((70, 1740, 1010, 1744), fill='#254056')
    d.rectangle((70, 1740, 70 + int(940 * (i + 1) / 9), 1744), fill=C_BLUE)

    im.save(WORK / f'scene{i:02d}.png')
    if i == 0:
        im.save(ROOT / '封面.png')


def ass_time(t):
    h = int(t // 3600)
    m = int(t // 60) % 60
    s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def tts_ok(p: Path) -> bool:
    """配音文件是否可用（存在且非空）。

    注意：edge-tts 偶发 NoAudioReceived 或被限流时，会留下 0 字节文件。
    只判断 exists() 会把这种残file当成已生成，后续 ffmpeg 直接失败，
    所以要同时校验大小。
    """
    try:
        return p.exists() and p.stat().st_size > 1024
    except OSError:
        return False


async def tts_save(text: str, out: Path, retries: int = 5) -> None:
    """带重试的配音生成。

    edge-tts 走微软在线服务，连续请求会被限流（NoAudioReceived）。
    这里失败就退避重试，并在每次重试前删掉可能的残file。
    """
    last = None
    for attempt in range(1, retries + 1):
        if out.exists():
            try:
                out.unlink()
            except OSError:
                pass
        try:
            await edge_tts.Communicate(text, 'zh-CN-YunxiNeural',
                                       rate='+12%').save(str(out))
            if tts_ok(out):
                return
            last = RuntimeError('生成的配音为空')
        except Exception as exc:      # NoAudioReceived / 网络抖断
            last = exc
        wait = 2.0 * attempt
        print(f'  TTS 第 {attempt}/{retries} 次失败（{last}），{wait:.0f}s 后重试', flush=True)
        await asyncio.sleep(wait)
    raise RuntimeError(f'配音生成失败：{out.name}（{last}）')


async def main():
    captions = []
    cursor = 0.0

    # 1) 场景图 + 配音 + 字幕时间轴
    for i, sc in enumerate(scenes):
        dur, title, hero, sub, voice = sc
        draw_scene(i, sc)

        src = WORK / f'voice{i:02d}.mp3'
        if not tts_ok(src):
            await tts_save(voice, src)

        ratio = probe(src) / (dur - 0.3)
        run(['-i', src, '-af', f'atempo={ratio:.6f},apad', '-t', dur,
             '-ar', '48000', '-ac', '1', WORK / f'voice{i:02d}.wav'])

        clauses = [c for c in voice.replace('。', '，').split('，') if c]
        total = sum(map(len, clauses))
        local = 0.0
        for c in clauses:
            length = (dur - 0.3) * len(c) / total
            captions.append((cursor + local, cursor + local + length, wrap(c, 18)))
            local += length
        cursor += dur
        print(f'Prepared {i + 1}/{len(scenes)}', flush=True)

    # 2) 字幕文件
    header = ('[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n'
              '[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,'
              'OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,'
              'Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,'
              'MarginV,Encoding\n'
              'Style: Default,Microsoft YaHei,46,&H00FFFFFF,&H00FFFFFF,&H00100A06,'
              '&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,75,75,285,1\n\n'
              '[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,'
              'Effect,Text\n')
    ass = header
    srt = ''

    def st(t):
        return (f'{int(t // 3600):02}:{int(t // 60) % 60:02}:{int(t) % 60:02},'
                f'{int(t % 1 * 1000):03}')

    for j, (start, end, c) in enumerate(captions):
        ass += (f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,'
                f'{c.replace(chr(10), chr(92) + "N")}\n')
        srt += f'{j + 1}\n{st(start)} --> {st(end)}\n{c}\n\n'

    (WORK / 'captions.ass').write_text(ass, encoding='utf-8-sig')
    (ROOT / '字幕.srt').write_text(srt, encoding='utf-8-sig')

    # 3) 逐场景渲染（缓慢放大 + 首尾淡入淡出）
    for i, sc in enumerate(scenes):
        dur = sc[0]
        out = WORK / f'clip{i:02d}.mp4'
        # 同样校验大小：中断留下的半截 mp4 不该被当成已渲染
        if not (out.exists() and out.stat().st_size > 10240):
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            run(['-loop', '1', '-i', WORK / f'scene{i:02d}.png',
                 '-i', WORK / f'voice{i:02d}.wav',
                 '-vf', ("zoompan=z='1+0.000035*on':x='iw/2-iw/zoom/2':"
                         "y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=30,"
                         f"fade=t=in:st=0:d=0.16,fade=t=out:st={dur - 0.16}:d=0.16"),
                 '-t', dur, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', out])
        print(f'Rendered {i + 1}/{len(scenes)}', flush=True)

    (WORK / 'concat.txt').write_text(
        '\n'.join(f"file 'clip{i:02d}.mp4'" for i in range(len(scenes))),
        encoding='utf-8')
    run(['-f', 'concat', '-safe', '0', '-i', WORK / 'concat.txt', '-c', 'copy',
         WORK / 'joined.mp4'])

    # 4) 原创电子节拍 BGM（不引用外部音乐）
    sr = 48000
    t = np.arange(int(cursor * sr)) / sr
    music = np.zeros_like(t)
    for k in range(int(cursor * 2)):
        start = int(k * .5 * sr)
        n = min(sr // 2, len(t) - start)
        q = np.arange(n) / sr
        freq = [220, 261.6256, 329.6276, 293.6648][(k // 8) % 4] * [1, 1.5, 2, 1.5][k % 4]
        music[start:start + n] += .032 * np.sin(2 * np.pi * freq * q) * np.exp(-q * 8)
        if k % 2 == 0:
            music[start:start + n] += .045 * np.sin(
                2 * np.pi * (48 * q + 2 * (1 - np.exp(-q * 25)))) * np.exp(-q * 22)
    music *= np.minimum(1, t / 1.5) * np.minimum(1, (cursor - t) / 2)
    with wave.open(str(WORK / '原创科技节奏.wav'), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((music * 32767).astype('<i2').tobytes())

    # 5) 混音 + 烧字幕
    run(['-i', WORK / 'joined.mp4', '-i', WORK / '原创科技节奏.wav',
         '-vf', 'ass=captions.ass',
         '-filter_complex',
         '[0:a]volume=1.5[v];[v][1:a]amix=inputs=2:duration=first:normalize=0,'
         'alimiter=limit=0.95[a]',
         '-map', '0:v', '-map', '[a]',
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19',
         '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
         '-t', cursor, '-movflags', '+faststart',
         ROOT / 'wechat-scheduled-sender_宣传版.mp4'])

    (ROOT / '定稿配音与时间轴.json').write_text(
        json.dumps(scenes, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DONE', cursor, flush=True)


if __name__ == '__main__':
    os.chdir(WORK)
    asyncio.run(main())