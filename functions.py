"""
本地工具表 + 运行时被 MCP tools/list 替换。

- 默认：本地演示函数（仅 print）
- 中控连上并 tools/list 后：func_map 被 MCP 工具替换，旧本地函数不再参与 FC
- MCP 断开：可恢复本地表
"""


def turn_on_light():
    print("turn_on_light")


def turn_off_light():
    print("turn_off_light")


def turn_on_ac():
    print("turn_on_ac")


def turn_off_ac():
    print("turn_off_ac")


def open_curtains():
    print("open_curtains")


def close_curtains():
    print("close_curtains")


def turn_on_tv():
    print("turn_on_tv")


def turn_off_tv():
    print("turn_off_tv")


def turn_on_air_purifier():
    print("turn_on_air_purifier")


def turn_off_air_purifier():
    print("turn_off_air_purifier")


def turn_on_water_heater():
    print("turn_on_water_heater")


def turn_off_water_heater():
    print("turn_off_water_heater")


def turn_on_robot_vacuum():
    print("turn_on_robot_vacuum")


def turn_off_robot_vacuum():
    print("turn_off_robot_vacuum")


def turn_on_fan():
    print("turn_on_fan")


def turn_off_fan():
    print("turn_off_fan")


def turn_on_humidifier():
    print("turn_on_humidifier")


def turn_off_humidifier():
    print("turn_off_humidifier")


def unlock_door():
    print("unlock_door")


def lock_door():
    print("lock_door")


def set_ac_temperature(value):
    print(f"set_ac_temperature(value={value})")


def set_light_brightness(value):
    print(f"set_light_brightness(value={value})")


def set_alarm(time):
    print(f"set_alarm(time='{time}')")


def set_speaker_volume(value):
    print(f"set_speaker_volume(value={value})")


def set_humidity(value):
    print(f"set_humidity(value={value})")


def set_fan_speed(level):
    print(f"set_fan_speed(level={level})")


def set_humidifier_level(value):
    print(f"set_humidifier_level(value={value})")


def set_curtain_openness(value):
    print(f"set_curtain_openness(value={value})")


def set_water_heater_temperature(value):
    print(f"set_water_heater_temperature(value={value})")


def set_robot_vacuum_mode(mode):
    print(f"set_robot_vacuum_mode(mode='{mode}')")


def set_ac_mode(mode):
    print(f"set_ac_mode(mode='{mode}')")


def set_light_color(color):
    print(f"set_light_color(color='{color}')")


def set_tv_volume(value):
    print(f"set_tv_volume(value={value})")


def set_air_purifier_speed(level):
    print(f"set_air_purifier_speed(level={level})")


def set_door_lock_password(password):
    print(f"set_door_lock_password(password='{password}')")


def set_curtain_timer(time):
    print(f"set_curtain_timer(time='{time}')")


def set_ac_timer(time):
    print(f"set_ac_timer(time='{time}')")


def set_light_timer(time):
    print(f"set_light_timer(time='{time}')")


def set_tv_channel(channel):
    print(f"set_tv_channel(channel={channel})")


def set_robot_vacuum_area(area):
    print(f"set_robot_vacuum_area(area='{area}')")


# 本地演示函数快照（MCP 替换前可恢复）
_local_func_map = {
    "turn_on_light": turn_on_light,
    "turn_off_light": turn_off_light,
    "turn_on_ac": turn_on_ac,
    "turn_off_ac": turn_off_ac,
    "open_curtains": open_curtains,
    "close_curtains": close_curtains,
    "turn_on_tv": turn_on_tv,
    "turn_off_tv": turn_off_tv,
    "turn_on_air_purifier": turn_on_air_purifier,
    "turn_off_air_purifier": turn_off_air_purifier,
    "turn_on_water_heater": turn_on_water_heater,
    "turn_off_water_heater": turn_off_water_heater,
    "turn_on_robot_vacuum": turn_on_robot_vacuum,
    "turn_off_robot_vacuum": turn_off_robot_vacuum,
    "turn_on_fan": turn_on_fan,
    "turn_off_fan": turn_off_fan,
    "turn_on_humidifier": turn_on_humidifier,
    "turn_off_humidifier": turn_off_humidifier,
    "unlock_door": unlock_door,
    "lock_door": lock_door,
    "set_ac_temperature": set_ac_temperature,
    "set_light_brightness": set_light_brightness,
    "set_alarm": set_alarm,
    "set_speaker_volume": set_speaker_volume,
    "set_humidity": set_humidity,
    "set_fan_speed": set_fan_speed,
    "set_humidifier_level": set_humidifier_level,
    "set_curtain_openness": set_curtain_openness,
    "set_water_heater_temperature": set_water_heater_temperature,
    "set_robot_vacuum_mode": set_robot_vacuum_mode,
    "set_ac_mode": set_ac_mode,
    "set_light_color": set_light_color,
    "set_tv_volume": set_tv_volume,
    "set_air_purifier_speed": set_air_purifier_speed,
    "set_door_lock_password": set_door_lock_password,
    "set_curtain_timer": set_curtain_timer,
    "set_ac_timer": set_ac_timer,
    "set_light_timer": set_light_timer,
    "set_tv_channel": set_tv_channel,
    "set_robot_vacuum_area": set_robot_vacuum_area,
}

# FC / 执行使用的当前表（可被 MCP 整体替换）
func_map = dict(_local_func_map)

# 标记：当前是否已被 MCP 工具表替换
mcp_replaced = False
# name -> 工具描述（来自 MCP）
mcp_tool_meta = {}


def _make_mcp_placeholder(name: str):
    """占位可调用对象：真实执行由 09 里 McpHost.call_tool 完成。"""

    def _placeholder(**kwargs):
        raise RuntimeError(
            f"MCP tool '{name}' 应由 McpHost.tools/call 执行，不要直接调本地占位"
        )

    _placeholder.__name__ = name
    _placeholder.__doc__ = f"MCP tool: {name}"
    return _placeholder


def replace_with_mcp_tools(tools) -> list[str]:
    """
    用 MCP tools/list 结果整体替换 func_map，旧本地家居函数不再参与 FC。

    tools: 可迭代，元素需有 .name / .description，或为 dict。
    """
    global mcp_replaced, mcp_tool_meta

    new_map = {}
    meta = {}
    for t in tools:
        if isinstance(t, dict):
            name = str(t.get("name", "") or "")
            desc = str(t.get("description", "") or "")
            schema = t.get("inputSchema") or t.get("input_schema") or {}
        else:
            name = str(getattr(t, "name", "") or "")
            desc = str(getattr(t, "description", "") or "")
            schema = getattr(t, "input_schema", None) or {}
        if not name:
            continue
        new_map[name] = _make_mcp_placeholder(name)
        meta[name] = {"description": desc, "inputSchema": schema}

    func_map.clear()
    func_map.update(new_map)
    mcp_tool_meta = meta
    mcp_replaced = True
    names = list(func_map.keys())
    print(f"[func_map] 已用 MCP 工具替换本地函数 → {names}")
    return names


def restore_local_functions() -> list[str]:
    """MCP 断开后恢复本地演示函数表。"""
    global mcp_replaced, mcp_tool_meta
    func_map.clear()
    func_map.update(_local_func_map)
    mcp_tool_meta = {}
    mcp_replaced = False
    names = list(func_map.keys())
    print(f"[func_map] 已恢复本地函数 ({len(names)} 个)")
    return names


def current_tool_names() -> list[str]:
    return list(func_map.keys())
