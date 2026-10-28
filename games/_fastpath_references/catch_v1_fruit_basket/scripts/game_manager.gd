extends Node

## Game lifecycle manager.
## Provides signals that the host app can connect to for game state changes.

signal game_started
signal game_over(score: int)
signal game_paused
signal game_resumed

var is_playing: bool = false
var score: int = 0


func start_game() -> void:
	is_playing = true
	score = 0
	game_started.emit()


func end_game(final_score: int = -1) -> void:
	is_playing = false
	if final_score >= 0:
		score = final_score
	game_over.emit(score)


func add_score(points: int) -> void:
	score += points
