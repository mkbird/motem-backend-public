extends Node

## Input bridge — receives body pose data from iOS host app via SwiftGodot.
## This file should NOT be modified by AI. It is part of the fixed template.

## Body pose values updated by the host app each frame.
var body_lean_x: float = 0.0   ## -1 ~ 1, left/right lean
var body_lean_y: float = 0.0   ## -1 ~ 1, forward/back lean
var is_reaching_out: bool = false
var is_ducking: bool = false
var is_hands_up: bool = false
var body_detected: bool = false

## Face tracking values.
var face_position: Vector2 = Vector2(0.5, 0.5)  ## normalized 0~1
var face_detected: bool = false

## Hand gesture.
var hand_gesture: String = "none"  ## none, heart, ok, thumbsUp, peace, wave, fist

var _connected: bool = false


func _ready() -> void:
	# SwiftGodotKit adds the bridge node after the scene loads, so we can't
	# connect in _ready(). _try_connect() is called every frame until it succeeds.
	_try_connect()


func _process(_delta: float) -> void:
	if not _connected:
		_try_connect()


func _try_connect() -> void:
	var host_bridge = get_node_or_null("/root/__swiftgodotkit_bridge__")
	if host_bridge and host_bridge.has_signal("messageFromHost"):
		host_bridge.messageFromHost.connect(_on_host_message)
		_connected = true
		print("[InputBridge] connected to __swiftgodotkit_bridge__ messageFromHost")
	# else: bridge not ready yet, will retry next frame


func _on_host_message(data: Dictionary) -> void:
	var msg_type: String = data.get("type", "")
	if msg_type == "body_pose":
		update_body_pose(data)
	elif msg_type == "face":
		update_face(data)


func update_body_pose(data: Dictionary) -> void:
	body_lean_x = data.get("body_lean_x", 0.0)
	body_lean_y = data.get("body_lean_y", 0.0)
	is_reaching_out = data.get("is_reaching_out", false)
	is_ducking = data.get("is_ducking", false)
	is_hands_up = data.get("is_hands_up", false)
	body_detected = data.get("body_detected", false)


func update_face(data: Dictionary) -> void:
	face_position = Vector2(
		data.get("face_x", 0.5),
		data.get("face_y", 0.5)
	)
	face_detected = data.get("face_detected", false)
	hand_gesture = data.get("hand_gesture", "none")
