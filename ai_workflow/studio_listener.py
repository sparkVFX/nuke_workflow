# -*- coding: utf-8 -*-
"""
NukeStudio端监听脚本
在NukeStudio的Script Editor中运行，持续监听Nuke发送过来的文件，
自动添加到当前项目的时间线轨道上。

使用方式:
  1. 在NukeStudio中打开/创建一个项目
  2. 在Script Editor中执行此脚本
  3. 监听会在后台线程运行，不阻塞UI
  4. 调用 stop_listener() 停止监听
"""

import hiero.core
import hiero.ui
import socket
import json
import struct
import threading
import os

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 54321

_listener_running = False
_server_socket = None


def find_or_create_sequence(project):
    """获取当前激活的序列，如果没有则创建一个"""
    # 尝试获取当前打开的序列
    seq = hiero.ui.activeSequence()
    if seq:
        return seq

    # 没有打开的序列，在项目根目录创建一个
    root = project.clipsBin()
    sequence = hiero.core.Sequence("AutoSequence")
    root.addItem(hiero.core.BinItem(sequence))
    return sequence


def _get_insert_time(sequence):
    """计算在序列所有轨道（视频+音频）末尾的插入位置"""
    max_time = 0
    for track in list(sequence.videoTracks()) + list(sequence.audioTracks()):
        for item in track.items():
            t_out = item.timelineOut() + 1
            if t_out > max_time:
                max_time = t_out
    return max_time


def add_clips_to_timeline(clips_data):
    """将接收到的clip数据添加到时间线（同时添加视频和音频轨道）"""
    projects = hiero.core.projects()
    if not projects:
        print("[Studio Listener] 没有打开的项目，无法添加。")
        return "错误: 没有打开的项目"

    project = projects[0]
    results = []

    # 获取或创建序列
    sequence = find_or_create_sequence(project)

    # 确保序列至少有一条视频轨道和一条音频轨道
    if not sequence.videoTracks():
        sequence.addTrack(hiero.core.VideoTrack("Video 1"))
    if not sequence.audioTracks():
        sequence.addTrack(hiero.core.AudioTrack("Audio 1"))

    # 始终使用第一条视频/音频轨道（索引0），让所有clip排在同一组轨道上
    video_track_index = 0
    audio_track_index = 0

    for clip_info in clips_data:
        file_path = clip_info["file"]
        clip_name = clip_info.get("name", os.path.basename(file_path))

        try:
            # 创建MediaSource和Clip
            media_source = hiero.core.MediaSource(file_path)
            clip = hiero.core.Clip(media_source)

            # 添加clip到项目Bin
            root_bin = project.clipsBin()
            bin_item = hiero.core.BinItem(clip)
            root_bin.addItem(bin_item)

            # 计算插入位置：所有轨道末尾
            insert_time = _get_insert_time(sequence)

            # 使用 Sequence.addClip —— 自动为视频和音频通道创建 TrackItem 并链接
            track_items = sequence.addClip(clip, insert_time,
                                           videoTrackIndex=video_track_index,
                                           audioTrackIndex=audio_track_index)

            # 统计添加的轨道类型
            added_types = set()
            if track_items:
                for ti in track_items:
                    parent_track = ti.parentTrack()
                    if parent_track:
                        if isinstance(parent_track, hiero.core.VideoTrack):
                            added_types.add("Video")
                        elif isinstance(parent_track, hiero.core.AudioTrack):
                            added_types.add("Audio")

            type_str = "+".join(sorted(added_types)) if added_types else "Unknown"
            msg = "已添加: {} [{}] (位置 {})".format(clip_name, type_str, insert_time)
            print("[Studio Listener] " + msg)
            results.append(msg)

        except Exception as e:
            err = "添加 {} 失败: {}".format(file_path, str(e))
            print("[Studio Listener] " + err)
            results.append(err)

    return "; ".join(results)


def handle_client(conn, addr):
    """处理单个客户端连接"""
    try:
        # 读取4字节长度头
        length_data = conn.recv(4)
        if not length_data or len(length_data) < 4:
            return
        msg_length = struct.unpack(">I", length_data)[0]

        # 读取完整数据
        data = b""
        while len(data) < msg_length:
            chunk = conn.recv(min(4096, msg_length - len(data)))
            if not chunk:
                break
            data += chunk

        payload = json.loads(data.decode("utf-8"))
        print("[Studio Listener] 收到数据: {}".format(payload.get("action", "unknown")))

        action = payload.get("action", "")
        if action == "add_clips":
            clips = payload.get("clips", [])
            # 在主线程中执行Hiero操作
            response = hiero.core.executeInMainThreadWithResult(lambda: add_clips_to_timeline(clips))
        else:
            response = "未知操作: {}".format(action)

        conn.sendall(response.encode("utf-8"))

    except Exception as e:
        print("[Studio Listener] 处理连接出错: {}".format(str(e)))
        try:
            conn.sendall("服务端错误: {}".format(str(e)).encode("utf-8"))
        except:
            pass
    finally:
        conn.close()


def listener_loop():
    """监听循环，在后台线程中运行"""
    global _listener_running, _server_socket

    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _server_socket.settimeout(2.0)  # 超时以便定期检查停止标志

    try:
        _server_socket.bind((LISTEN_HOST, LISTEN_PORT))
        _server_socket.listen(5)
        print("[Studio Listener] 监听已启动 {}:{}".format(LISTEN_HOST, LISTEN_PORT))

        while _listener_running:
            try:
                conn, addr = _server_socket.accept()
                print("[Studio Listener] 收到连接: {}".format(addr))
                # 在新线程中处理，避免阻塞监听
                t = threading.Thread(target=handle_client, args=(conn, addr))
                t.daemon = True
                t.start()
            except socket.timeout:
                continue  # 超时后回到循环检查 _listener_running

    except Exception as e:
        print("[Studio Listener] 监听错误: {}".format(str(e)))
    finally:
        if _server_socket:
            _server_socket.close()
            _server_socket = None
        print("[Studio Listener] 监听已停止。")


def start_listener():
    """启动监听（在后台线程中运行）"""
    global _listener_running

    if _listener_running:
        print("[Studio Listener] 监听已在运行中。")
        return

    _listener_running = True
    t = threading.Thread(target=listener_loop)
    t.daemon = True
    t.start()
    print("[Studio Listener] 后台监听线程已启动。")


def stop_listener():
    """停止监听"""
    global _listener_running
    _listener_running = False
    print("[Studio Listener] 正在停止监听...")


# ---- 直接执行时启动监听 ----
start_listener()
