/* game.js — Pick Pocket Game main logic */

(function () {
    'use strict';

    // ── Constants ──

    var CHAR_SIZE = 60;
    var STEAL_RANGE = 90;
    var GUARD_DETECT_RANGE = 180;
    var GUARD_CHASE_RANGE = 280;
    var PLAYER_SPEED = 2.5;
    var SPRINT_SPEED = 5;
    var SPRINT_DURATION = 3;
    var SPRINT_COOLDOWN = 5;
    var DISGUISE_DURATION = 2;
    var DISGUISE_COOLDOWN = 6;
    var INVINCIBLE_DURATION = 2;
    var WANTED_DECAY = 3;        // per second
    var WANTED_CHASE_THRESHOLD = 50;
    var WANTED_MAX_THRESHOLD = 80;
    var INPUT_SMOOTHING = 0.12;
    var DEAD_ZONE = 0.04;

    var NPC_TYPES = [
        { type: 'tourist',  value: 100, stealTime: 2.0, alertRange: 40 },
        { type: 'merchant', value: 300, stealTime: 1.5, alertRange: 50 },
        { type: 'lady',     value: 500, stealTime: 1.0, alertRange: 60 },
        { type: 'boss',     value: 800, stealTime: 0.8, alertRange: 70 }
    ];

    var STAGES = [
        { score: 0,    npcCount: 3, security: 1, undercover: 0 },
        { score: 1000, npcCount: 4, security: 2, undercover: 0 },
        { score: 3000, npcCount: 5, security: 2, undercover: 1 },
        { score: 6000, npcCount: 6, security: 3, undercover: 2 }
    ];

    // ── Canvas Setup ──

    var canvas = document.getElementById('game');
    var ctx = canvas.getContext('2d');
    var W, H;
    var dpr = window.devicePixelRatio || 1;

    function resize() {
        W = window.innerWidth;
        H = window.innerHeight;
        canvas.width = W * dpr;
        canvas.height = H * dpr;
        canvas.style.width = W + 'px';
        canvas.style.height = H + 'px';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    window.addEventListener('resize', resize);
    resize();

    // ── Game State ──

    var phase = 'ready';  // ready | calibrating | playing | gameOver
    var score = 0;
    var lives = 3;
    var highScore = parseInt(localStorage.getItem('pickpocket_high') || '0');
    var isNewHigh = false;
    var wantedLevel = 0;
    var frame = 0;
    var gameTime = 0;
    var stage = 0;

    // Player
    var player = { x: 0, y: 0, smoothX: 0, smoothY: 0, facingLeft: false, state: 'normal' };

    // Calibration
    var calibrationTimer = 0;
    var CALIBRATION_DURATION = 2;

    // Stealing
    var stealTarget = -1;
    var stealProgress = 0;

    // Abilities
    var sprintTimer = 0;
    var sprintCooldown = 0;
    var disguiseTimer = 0;
    var disguiseCooldown = 0;
    var invincibleTimer = 0;

    // Entities
    var npcs = [];
    var guards = [];
    var lootParticles = [];
    var floatingTexts = [];
    var scenery = [];

    // Timing
    var lastTime = 0;

    // ── Entity Creation ──

    function createNPC(w, h) {
        var info = NPC_TYPES[Math.floor(Math.random() * NPC_TYPES.length)];
        return {
            x: Math.random() * (w - 100) + 50,
            y: Math.random() * (h - 100) + 50,
            vx: (Math.random() - 0.5) * 0.8,
            vy: (Math.random() - 0.5) * 0.8,
            type: info.type,
            value: info.value,
            stealTime: info.stealTime,
            alertRange: info.alertRange,
            isRobbed: false,
            alertLevel: 0,
            walkTimer: Math.random() * 3,
            facingLeft: Math.random() > 0.5
        };
    }

    function createGuard(type, w, h) {
        var pts = [];
        var cx = Math.random() * (w - 200) + 100;
        var cy = Math.random() * (h - 200) + 100;
        for (var i = 0; i < 4; i++) {
            pts.push({
                x: cx + (Math.random() - 0.5) * 200,
                y: cy + (Math.random() - 0.5) * 200
            });
        }
        return {
            x: pts[0].x,
            y: pts[0].y,
            type: type,  // 'security' or 'undercover'
            patrolPath: pts,
            pathIndex: 0,
            isChasing: false,
            isRevealed: false,
            speed: type === 'security' ? 1.2 : 1.0,
            chaseSpeed: type === 'security' ? 2.8 : 2.5,
            facingLeft: false
        };
    }

    function createScenery(w, h) {
        scenery = [];
        var count = Math.floor((w * h) / 40000);
        for (var i = 0; i < count; i++) {
            var types = ['tree', 'lamp', 'bench'];
            scenery.push({
                type: types[Math.floor(Math.random() * types.length)],
                x: Math.random() * w,
                y: Math.random() * h
            });
        }
    }

    // ── Initialize Stage ──

    function initStage(stageIndex) {
        var s = STAGES[stageIndex];
        npcs = [];
        guards = [];
        for (var i = 0; i < s.npcCount; i++) {
            npcs.push(createNPC(W, H));
        }
        for (var j = 0; j < s.security; j++) {
            guards.push(createGuard('security', W, H));
        }
        for (var k = 0; k < s.undercover; k++) {
            guards.push(createGuard('undercover', W, H));
        }
        createScenery(W, H);
    }

    function startGame() {
        score = 0;
        lives = 3;
        wantedLevel = 0;
        stealTarget = -1;
        stealProgress = 0;
        sprintTimer = 0;
        sprintCooldown = 0;
        disguiseTimer = 0;
        disguiseCooldown = 0;
        invincibleTimer = 0;
        lootParticles = [];
        floatingTexts = [];
        isNewHigh = false;
        stage = 0;
        player.x = W / 2;
        player.y = H / 2;
        player.smoothX = W / 2;
        player.smoothY = H / 2;
        player.state = 'normal';
        frame = 0;
        gameTime = 0;
        phase = 'calibrating';
        calibrationTimer = 0;
        initStage(0);
    }

    // ── Update ──

    function update(dt) {
        if (phase === 'calibrating') {
            calibrationTimer += dt;
            if (calibrationTimer >= CALIBRATION_DURATION) {
                phase = 'playing';
            }
            return;
        }
        if (phase !== 'playing') return;

        var input = window.getInput();
        gameTime += dt;
        frame += dt;

        // ── Timers ──
        if (sprintTimer > 0) sprintTimer -= dt;
        if (sprintCooldown > 0) sprintCooldown -= dt;
        if (disguiseTimer > 0) {
            disguiseTimer -= dt;
            if (disguiseTimer <= 0) player.state = 'normal';
        }
        if (disguiseCooldown > 0) disguiseCooldown -= dt;
        if (invincibleTimer > 0) invincibleTimer -= dt;

        // ── Player Movement ──
        // Landscape: Vision X↔Y axes are swapped vs screen X↔Y (same as MeetingSurvivorGame).
        // bodyLeanY (Vision Y / forward-backward) → screen X  — amplify 2× since maxOffsetY=0.1 is tight
        // bodyLeanX (Vision X / left-right)       → screen Y
        var rawLeanX = input.bodyLeanY || 0;   // screen horizontal
        var rawLeanY = input.bodyLeanX || 0;   // screen vertical
        rawLeanX = rawLeanX * 2;               // compensate lower sensitivity of bodyLeanY
        rawLeanX = Math.max(-1, Math.min(1, rawLeanX));
        var leanX = Math.abs(rawLeanX) < DEAD_ZONE ? 0 : rawLeanX;
        var leanY = Math.abs(rawLeanY) < DEAD_ZONE ? 0 : rawLeanY;

        var speed = sprintTimer > 0 ? SPRINT_SPEED : PLAYER_SPEED;
        var targetX = player.x + leanX * speed * 60 * dt;
        var targetY = player.y + leanY * speed * 60 * dt;

        // Clamp to screen
        targetX = Math.max(CHAR_SIZE, Math.min(W - CHAR_SIZE, targetX));
        targetY = Math.max(CHAR_SIZE, Math.min(H - CHAR_SIZE, targetY));

        player.x = targetX;
        player.y = targetY;
        player.smoothX += (player.x - player.smoothX) * INPUT_SMOOTHING;
        player.smoothY += (player.y - player.smoothY) * INPUT_SMOOTHING;

        if (leanX < -0.05) player.facingLeft = true;
        else if (leanX > 0.05) player.facingLeft = false;

        // ── Abilities ──
        // Sprint (duck)
        if (input.isDucking && sprintCooldown <= 0 && sprintTimer <= 0) {
            sprintTimer = SPRINT_DURATION;
            sprintCooldown = SPRINT_COOLDOWN + SPRINT_DURATION;
            player.state = 'sprint';
            addFloatingText(player.x, player.y - 30, 'SPRINT!', '#00e5ff');
        }
        if (sprintTimer <= 0 && player.state === 'sprint') {
            player.state = 'normal';
        }

        // Disguise (hands up)
        if (input.isHandsUp && disguiseCooldown <= 0 && disguiseTimer <= 0) {
            disguiseTimer = DISGUISE_DURATION;
            disguiseCooldown = DISGUISE_COOLDOWN + DISGUISE_DURATION;
            player.state = 'disguise';
            wantedLevel = Math.max(0, wantedLevel - 20);
            addFloatingText(player.x, player.y - 30, 'DISGUISE!', '#ab47bc');
        }

        // ── Stealing ──
        if (input.isReachingOut && disguiseTimer <= 0) {
            // Find nearest stealable NPC
            var nearest = -1;
            var nearDist = STEAL_RANGE;
            for (var i = 0; i < npcs.length; i++) {
                if (npcs[i].isRobbed) continue;
                var dx = npcs[i].x - player.x;
                var dy = npcs[i].y - player.y;
                var dist = Math.sqrt(dx * dx + dy * dy);
                if (dist < nearDist) {
                    nearDist = dist;
                    nearest = i;
                }
            }
            if (nearest >= 0) {
                if (stealTarget !== nearest) {
                    stealTarget = nearest;
                    stealProgress = 0;
                }
                stealProgress += dt / npcs[nearest].stealTime;
                player.state = 'stealing';

                if (stealProgress >= 1) {
                    // Success!
                    var npc = npcs[nearest];
                    npc.isRobbed = true;
                    score += npc.value;
                    wantedLevel = Math.min(100, wantedLevel + 10 + Math.floor(npc.value / 100) * 5);
                    addFloatingText(player.x, player.y - 40, '+$' + npc.value, '#4caf50');
                    addLootParticle(npc.x, npc.y, player.x, player.y, npc.type);
                    stealTarget = -1;
                    stealProgress = 0;
                    player.state = 'normal';
                    checkStageUp();
                    respawnNPC(nearest);
                }
            } else {
                stealTarget = -1;
                stealProgress = 0;
                if (player.state === 'stealing') player.state = 'normal';
            }
        } else {
            if (stealTarget >= 0 && stealProgress > 0 && stealProgress < 1) {
                // Interrupted steal — alert NPC
                if (stealTarget < npcs.length && !npcs[stealTarget].isRobbed) {
                    npcs[stealTarget].alertLevel = 1;
                    wantedLevel = Math.min(100, wantedLevel + 15);
                    addFloatingText(npcs[stealTarget].x, npcs[stealTarget].y - 30, '!', '#f44336');
                }
            }
            stealTarget = -1;
            stealProgress = 0;
            if (player.state === 'stealing') player.state = 'normal';
        }

        // ── NPC Update ──
        for (var n = 0; n < npcs.length; n++) {
            var npc = npcs[n];
            npc.walkTimer -= dt;
            if (npc.walkTimer <= 0) {
                npc.vx = (Math.random() - 0.5) * 1.2;
                npc.vy = (Math.random() - 0.5) * 1.2;
                npc.walkTimer = 2 + Math.random() * 3;
            }
            npc.x += npc.vx * 60 * dt;
            npc.y += npc.vy * 60 * dt;
            // Bounce off walls
            if (npc.x < 30 || npc.x > W - 30) npc.vx *= -1;
            if (npc.y < 30 || npc.y > H - 30) npc.vy *= -1;
            npc.x = Math.max(30, Math.min(W - 30, npc.x));
            npc.y = Math.max(30, Math.min(H - 30, npc.y));
            npc.facingLeft = npc.vx < 0;

            // Alert decay
            if (npc.alertLevel > 0) npc.alertLevel = Math.max(0, npc.alertLevel - dt * 0.3);
        }

        // ── Guard Update ──
        var shouldChase = wantedLevel >= WANTED_CHASE_THRESHOLD && disguiseTimer <= 0;
        for (var g = 0; g < guards.length; g++) {
            var guard = guards[g];
            var gdx = player.x - guard.x;
            var gdy = player.y - guard.y;
            var gdist = Math.sqrt(gdx * gdx + gdy * gdy);

            if (shouldChase && gdist < GUARD_CHASE_RANGE) {
                guard.isChasing = true;
            } else if (!shouldChase || gdist > GUARD_CHASE_RANGE * 2) {
                guard.isChasing = false;
            }

            // Reveal undercover when close
            if (guard.type === 'undercover' && gdist < 80) {
                guard.isRevealed = true;
            }

            if (guard.isChasing) {
                var cspeed = guard.chaseSpeed * (wantedLevel >= WANTED_MAX_THRESHOLD ? 1.3 : 1);
                var angle = Math.atan2(gdy, gdx);
                guard.x += Math.cos(angle) * cspeed * 60 * dt;
                guard.y += Math.sin(angle) * cspeed * 60 * dt;
                guard.facingLeft = gdx < 0;
            } else {
                // Patrol
                var pt = guard.patrolPath[guard.pathIndex];
                var pdx = pt.x - guard.x;
                var pdy = pt.y - guard.y;
                var pdist = Math.sqrt(pdx * pdx + pdy * pdy);
                if (pdist < 5) {
                    guard.pathIndex = (guard.pathIndex + 1) % guard.patrolPath.length;
                } else {
                    var pangle = Math.atan2(pdy, pdx);
                    guard.x += Math.cos(pangle) * guard.speed * 60 * dt;
                    guard.y += Math.sin(pangle) * guard.speed * 60 * dt;
                    guard.facingLeft = pdx < 0;
                }
            }

            // Collision with player
            if (guard.isChasing && gdist < 30 && invincibleTimer <= 0 && disguiseTimer <= 0) {
                lives--;
                invincibleTimer = INVINCIBLE_DURATION;
                wantedLevel = Math.max(0, wantedLevel - 30);
                addFloatingText(player.x, player.y - 40, 'CAUGHT!', '#f44336');

                // Reset guards
                for (var rg = 0; rg < guards.length; rg++) {
                    guards[rg].isChasing = false;
                }

                if (lives <= 0) {
                    gameOver();
                    return;
                }
            }
        }

        // ── Wanted Decay ──
        wantedLevel = Math.max(0, wantedLevel - WANTED_DECAY * dt);

        // ── Particles ──
        for (var p = lootParticles.length - 1; p >= 0; p--) {
            var lp = lootParticles[p];
            lp.life -= dt;
            var t = 1 - lp.life / lp.maxLife;
            lp.x = lp.startX + (lp.endX - lp.startX) * t;
            lp.y = lp.startY + (lp.endY - lp.startY) * t - Math.sin(t * Math.PI) * 30;
            if (lp.life <= 0) lootParticles.splice(p, 1);
        }

        // ── Floating Texts ──
        for (var f = floatingTexts.length - 1; f >= 0; f--) {
            floatingTexts[f].y -= 40 * dt;
            floatingTexts[f].life -= dt;
            if (floatingTexts[f].life <= 0) floatingTexts.splice(f, 1);
        }
    }

    // ── Helpers ──

    function checkStageUp() {
        for (var i = STAGES.length - 1; i >= 0; i--) {
            if (score >= STAGES[i].score && i > stage) {
                stage = i;
                initStage(i);
                addFloatingText(W / 2, H / 2, 'STAGE ' + (i + 1) + '!', '#ffeb3b');
                break;
            }
        }
    }

    function respawnNPC(index) {
        // Respawn after a delay — replace with new NPC
        setTimeout(function () {
            if (index < npcs.length) {
                npcs[index] = createNPC(W, H);
            }
        }, 3000);
    }

    function addLootParticle(sx, sy, ex, ey, type) {
        lootParticles.push({
            startX: sx, startY: sy, endX: ex, endY: ey,
            x: sx, y: sy, type: type,
            life: 0.6, maxLife: 0.6
        });
    }

    function addFloatingText(x, y, text, color) {
        floatingTexts.push({ x: x, y: y, text: text, color: color, life: 1.2 });
    }

    function gameOver() {
        phase = 'gameOver';
        if (score > highScore) {
            highScore = score;
            isNewHigh = true;
            localStorage.setItem('pickpocket_high', String(score));
        }
        // Notify native
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.gameEvent) {
            window.webkit.messageHandlers.gameEvent.postMessage({
                type: 'gameOver', score: score, highScore: highScore, isNewHigh: isNewHigh
            });
        }
    }

    // ── Render ──

    function render() {
        ctx.clearRect(0, 0, W, H);

        if (phase === 'ready') {
            renderReady();
            return;
        }
        if (phase === 'gameOver') {
            renderPlaying(); // background
            renderGameOver();
            return;
        }

        renderPlaying();

        if (phase === 'calibrating') {
            renderCalibration();
        }
    }

    function renderPlaying() {
        // Background — dark street
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, W, H);

        // Grid lines (pavement)
        ctx.strokeStyle = 'rgba(255,255,255,0.03)';
        ctx.lineWidth = 1;
        for (var gx = 0; gx < W; gx += 60) {
            ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, H); ctx.stroke();
        }
        for (var gy = 0; gy < H; gy += 60) {
            ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
        }

        // Scenery
        for (var si = 0; si < scenery.length; si++) {
            var sc = scenery[si];
            if (sc.type === 'tree') Sprites.drawTree(ctx, sc.x, sc.y, 40);
            else if (sc.type === 'lamp') Sprites.drawLamp(ctx, sc.x, sc.y, 40);
            else if (sc.type === 'bench') Sprites.drawBench(ctx, sc.x, sc.y, 40);
        }

        // NPCs
        for (var ni = 0; ni < npcs.length; ni++) {
            var npc = npcs[ni];
            if (npc.isRobbed) continue;

            var drawFn;
            switch (npc.type) {
                case 'tourist': drawFn = Sprites.drawTourist; break;
                case 'merchant': drawFn = Sprites.drawMerchant; break;
                case 'lady': drawFn = Sprites.drawLady; break;
                case 'boss': drawFn = Sprites.drawBoss; break;
            }
            if (drawFn) drawFn(ctx, npc.x, npc.y, CHAR_SIZE, frame, npc.facingLeft);

            // Alert indicator
            if (npc.alertLevel > 0) {
                Sprites.drawAlertBar(ctx, npc.x, npc.y - CHAR_SIZE * 0.7, 30, npc.alertLevel, 1);
            }

            // Steal prompt
            var dx = npc.x - player.x;
            var dy = npc.y - player.y;
            var dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < STEAL_RANGE && !npc.isRobbed) {
                ctx.font = '16px sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText('\u270B', npc.x, npc.y - CHAR_SIZE * 0.8);
            }

            // Steal progress ring
            if (stealTarget === ni && stealProgress > 0) {
                Sprites.drawProgressRing(ctx, npc.x, npc.y - CHAR_SIZE * 0.6, 12, stealProgress, '#4caf50');
            }
        }

        // Guards
        for (var gi = 0; gi < guards.length; gi++) {
            var guard = guards[gi];
            if (guard.type === 'security') {
                Sprites.drawSecurity(ctx, guard.x, guard.y, CHAR_SIZE, frame, guard.facingLeft, guard.isChasing);
            } else {
                Sprites.drawUndercover(ctx, guard.x, guard.y, CHAR_SIZE, frame, guard.facingLeft, guard.isChasing, guard.isRevealed);
            }

            // Detection range (subtle)
            if (guard.isChasing) {
                ctx.strokeStyle = 'rgba(244,67,54,0.15)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.arc(guard.x, guard.y, GUARD_DETECT_RANGE, 0, Math.PI * 2);
                ctx.stroke();
            }
        }

        // Player
        var playerAlpha = invincibleTimer > 0 ? (Math.sin(frame * 20) > 0 ? 1 : 0.3) : 1;
        ctx.save();
        ctx.globalAlpha = playerAlpha;
        Sprites.drawThief(ctx, player.smoothX, player.smoothY, CHAR_SIZE, frame, player.facingLeft, player.state);
        ctx.restore();

        // Loot particles
        for (var li = 0; li < lootParticles.length; li++) {
            var lp = lootParticles[li];
            ctx.globalAlpha = lp.life / lp.maxLife;
            Sprites.drawLoot(ctx, lp.x, lp.y, 20, lp.type);
            ctx.globalAlpha = 1;
        }

        // Floating texts
        for (var fi = 0; fi < floatingTexts.length; fi++) {
            var ft = floatingTexts[fi];
            ctx.globalAlpha = ft.life / 1.2;
            ctx.fillStyle = ft.color;
            ctx.font = 'bold 18px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(ft.text, ft.x, ft.y);
            ctx.globalAlpha = 1;
        }

        // ── HUD ──
        renderHUD();
    }

    // ── UI Helpers ──

    var CYAN = '#66f3f3';
    var CARD_BG = 'rgba(255,255,255,0.07)';
    var CARD_BORDER = 'rgba(255,255,255,0.10)';

    function drawCard(x, y, w, h, r) {
        ctx.save();
        ctx.fillStyle = CARD_BG;
        ctx.strokeStyle = CARD_BORDER;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, r || 12);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
    }

    function renderHUD() {
        var pad = 20;
        ctx.save();
        ctx.textBaseline = 'middle';

        // ── Top-left: score card ──
        var scoreCardW = 140, scoreCardH = 44;
        drawCard(pad, pad, scoreCardW, scoreCardH);
        ctx.fillStyle = CYAN;
        ctx.font = 'bold 18px system-ui, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText('$' + score, pad + 12, pad + scoreCardH / 2);
        ctx.fillStyle = 'rgba(255,255,255,0.35)';
        ctx.font = '11px system-ui, sans-serif';
        ctx.fillText('BEST $' + highScore, pad + 12, pad + scoreCardH / 2 + 14);

        // ── Top-right: lives + stage ──
        var livesStr = '';
        for (var i = 0; i < lives; i++) livesStr += '♥ ';
        var rightCardW = 110, rightCardH = 44;
        drawCard(W - pad - rightCardW, pad, rightCardW, rightCardH);
        ctx.fillStyle = '#f87171';
        ctx.font = '15px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(livesStr.trim(), W - pad - rightCardW / 2, pad + rightCardH / 2 - 6);
        ctx.fillStyle = 'rgba(255,255,255,0.4)';
        ctx.font = '11px system-ui, sans-serif';
        ctx.fillText('STAGE ' + (stage + 1), W - pad - rightCardW / 2, pad + rightCardH / 2 + 10);

        // ── Top-center: wanted bar ──
        var barW = Math.min(220, W * 0.28);
        var barH = 8;
        var barX = W / 2 - barW / 2;
        var barY = pad + 8;
        // track
        ctx.fillStyle = 'rgba(255,255,255,0.12)';
        ctx.beginPath();
        ctx.roundRect(barX, barY, barW, barH, 4);
        ctx.fill();
        // fill
        var wRatio = wantedLevel / 100;
        if (wRatio > 0) {
            var wColor = wRatio < 0.5 ? '#fb923c' : '#f87171';
            ctx.fillStyle = wColor;
            ctx.beginPath();
            ctx.roundRect(barX, barY, barW * wRatio, barH, 4);
            ctx.fill();
        }
        ctx.fillStyle = 'rgba(255,255,255,0.45)';
        ctx.font = '10px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText('WANTED', W / 2, barY + barH + 5);

        // ── Bottom-left: ability cooldowns ──
        var cdY = H - pad - 44;
        drawCooldownIcon(ctx, pad, cdY, '🏃', sprintCooldown, SPRINT_COOLDOWN + SPRINT_DURATION, sprintTimer > 0, '低头冲刺');
        drawCooldownIcon(ctx, pad + 68, cdY, '🙌', disguiseCooldown, DISGUISE_COOLDOWN + DISGUISE_DURATION, disguiseTimer > 0, '举手伪装');

        // ── Body not detected warning ──
        var input = window.getInput();
        if (!input.bodyDetected) {
            ctx.fillStyle = 'rgba(251,146,60,0.9)';
            ctx.font = '13px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('⚠ 请将身体对准摄像头', W / 2, H - pad - 8);
        }

        ctx.restore();
    }

    function drawCooldownIcon(ctx, x, y, emoji, cooldown, maxCooldown, isActive, label) {
        ctx.save();
        var size = 44;
        ctx.fillStyle = isActive ? 'rgba(102,243,243,0.18)' : 'rgba(255,255,255,0.08)';
        ctx.strokeStyle = isActive ? 'rgba(102,243,243,0.5)' : 'rgba(255,255,255,0.10)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(x, y, size, size, 10);
        ctx.fill();
        ctx.stroke();

        ctx.font = '22px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.globalAlpha = (cooldown > 0 && !isActive) ? 0.35 : 1;
        ctx.fillText(emoji, x + size / 2, y + size / 2);
        ctx.globalAlpha = 1;

        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = cooldown > 0 && !isActive ? '#f87171' : 'rgba(255,255,255,0.5)';
        ctx.textBaseline = 'top';
        ctx.fillText(cooldown > 0 && !isActive ? Math.ceil(cooldown) + 's' : label, x + size / 2, y + size + 4);
        ctx.restore();
    }

    function renderCalibration() {
        ctx.save();
        ctx.fillStyle = 'rgba(0,0,0,0.72)';
        ctx.fillRect(0, 0, W, H);

        var cardW = 280, cardH = 160;
        var cx = W / 2, cy = H / 2;
        drawCard(cx - cardW / 2, cy - cardH / 2, cardW, cardH, 16);

        ctx.textAlign = 'center';
        ctx.fillStyle = '#fff';
        ctx.font = 'bold 17px system-ui, sans-serif';
        ctx.textBaseline = 'middle';
        ctx.fillText('🎯 身体校准', cx, cy - 36);

        ctx.fillStyle = 'rgba(255,255,255,0.5)';
        ctx.font = '13px system-ui, sans-serif';
        ctx.fillText('请正坐面对摄像头…', cx, cy - 6);

        var remaining = Math.ceil(CALIBRATION_DURATION - calibrationTimer);
        ctx.font = 'bold 44px system-ui, sans-serif';
        ctx.fillStyle = CYAN;
        ctx.fillText(String(remaining), cx, cy + 40);

        ctx.restore();
    }

    function renderReady() {
        ctx.save();
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, W, H);

        var cx = W / 2, cy = H / 2;
        var cardW = Math.min(400, W - 80), cardH = 260;

        drawCard(cx - cardW / 2, cy - cardH / 2, cardW, cardH, 20);

        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        // Icon
        ctx.font = '40px sans-serif';
        ctx.fillText('🥷', cx, cy - cardH / 2 + 52);

        // Title
        ctx.fillStyle = '#fff';
        ctx.font = 'bold 26px system-ui, sans-serif';
        ctx.fillText('神偷大盗', cx, cy - cardH / 2 + 102);

        // Subtitle
        ctx.fillStyle = 'rgba(255,255,255,0.45)';
        ctx.font = '13px system-ui, sans-serif';
        ctx.fillText('身体控制 · 偷窃与逃脱', cx, cy - cardH / 2 + 128);

        // Controls (compact)
        ctx.font = '12px system-ui, sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.35)';
        var hints = ['↔ 身体倾斜移动', '💪 单手伸出偷窃', '⬇ 低头冲刺  🙌 举手伪装'];
        for (var ci = 0; ci < hints.length; ci++) {
            ctx.fillText(hints[ci], cx, cy - cardH / 2 + 158 + ci * 20);
        }

        // High score
        if (highScore > 0) {
            ctx.fillStyle = '#fbbf24';
            ctx.font = 'bold 13px system-ui, sans-serif';
            ctx.fillText('🏆 最高分 $' + highScore, cx, cy + cardH / 2 - 44);
        }

        // Start button
        var btnW = 140, btnH = 38;
        ctx.fillStyle = CYAN;
        ctx.beginPath();
        ctx.roundRect(cx - btnW / 2, cy + cardH / 2 - 26, btnW, btnH, 10);
        ctx.fill();
        ctx.fillStyle = '#0a1a1a';
        ctx.font = 'bold 15px system-ui, sans-serif';
        ctx.fillText('开始游戏', cx, cy + cardH / 2 - 26 + btnH / 2);

        ctx.restore();
    }

    function renderGameOver() {
        ctx.save();
        ctx.fillStyle = 'rgba(0,0,0,0.78)';
        ctx.fillRect(0, 0, W, H);

        var cx = W / 2, cy = H / 2;
        var cardW = Math.min(320, W - 80), cardH = 200;

        drawCard(cx - cardW / 2, cy - cardH / 2, cardW, cardH, 20);

        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        ctx.fillStyle = '#f87171';
        ctx.font = 'bold 20px system-ui, sans-serif';
        ctx.fillText('游戏结束', cx, cy - cardH / 2 + 40);

        ctx.fillStyle = CYAN;
        ctx.font = 'bold 36px system-ui, sans-serif';
        ctx.fillText('$' + score, cx, cy - cardH / 2 + 90);

        if (isNewHigh) {
            ctx.fillStyle = '#fbbf24';
            ctx.font = 'bold 14px system-ui, sans-serif';
            ctx.fillText('🌟 新纪录！', cx, cy - cardH / 2 + 126);
        } else {
            ctx.fillStyle = 'rgba(255,255,255,0.35)';
            ctx.font = '13px system-ui, sans-serif';
            ctx.fillText('最高分 $' + highScore, cx, cy - cardH / 2 + 126);
        }

        // Restart button
        var blink = Math.sin(Date.now() / 450) > 0;
        var btnW = 130, btnH = 36;
        ctx.fillStyle = blink ? CYAN : 'rgba(102,243,243,0.7)';
        ctx.beginPath();
        ctx.roundRect(cx - btnW / 2, cy + cardH / 2 - 50, btnW, btnH, 10);
        ctx.fill();
        ctx.fillStyle = '#0a1a1a';
        ctx.font = 'bold 14px system-ui, sans-serif';
        ctx.fillText('再来一局', cx, cy + cardH / 2 - 50 + btnH / 2);

        ctx.restore();
    }

    // ── Game Loop ──

    function loop(timestamp) {
        var dt = lastTime ? Math.min((timestamp - lastTime) / 1000, 0.05) : 0.016;
        lastTime = timestamp;

        update(dt);
        render();
        requestAnimationFrame(loop);
    }

    // ── Keyboard fallback for browser testing ──
    var keys = {};
    window.addEventListener('keydown', function (e) {
        keys[e.key] = true;
        applyKeyboard();
    });
    window.addEventListener('keyup', function (e) {
        keys[e.key] = false;
        applyKeyboard();
    });

    function applyKeyboard() {
        var lx = 0, ly = 0;
        if (keys['ArrowLeft'] || keys['a']) lx = -0.5;
        if (keys['ArrowRight'] || keys['d']) lx = 0.5;
        if (keys['ArrowUp'] || keys['w']) ly = -0.5;
        if (keys['ArrowDown'] || keys['s']) ly = 0.5;

        window._gameInput = {
            bodyLeanX: lx,
            bodyLeanY: ly,
            isReachingOut: keys[' '] || false,
            isDucking: keys['Shift'] || false,
            isHandsUp: keys['q'] || false,
            bodyDetected: true
        };
    }

    // ── Tap/Click to start ──
    canvas.addEventListener('click', function () {
        if (phase === 'ready' || phase === 'gameOver') {
            startGame();
        }
    });
    canvas.addEventListener('touchend', function (e) {
        e.preventDefault();
        if (phase === 'ready' || phase === 'gameOver') {
            startGame();
        }
    });

    // ── Start ──
    requestAnimationFrame(loop);
})();
