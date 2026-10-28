/* sprites.js — Canvas character drawing (Q-style simple art) */

var Sprites = (function () {
    'use strict';

    // ── Color Palettes ──

    var THIEF = { hood: '#222', mask: '#333', eye: '#fff', pupil: '#111', body: '#2a2a2a', pants: '#1a1a1a', skin: '#f0c8a0' };
    var TOURIST = { shirt: '#f5d442', hat: '#42a5f5', shorts: '#42a5f5', skin: '#f0c8a0', camera: '#666' };
    var MERCHANT = { suit: '#555', tie: '#c62828', shirt: '#fff', case_: '#8d6e63', skin: '#f0c8a0' };
    var LADY = { dress: '#9c27b0', pearls: '#ffd700', bag: '#ce93d8', skin: '#f0c8a0', hair: '#5d4037' };
    var BOSS = { coat: '#1a1a1a', shades: '#333', watch: '#ffd700', skin: '#f0c8a0', shirt: '#fff' };
    var SECURITY = { uniform: '#1a237e', hat: '#0d47a1', badge: '#ffd700', skin: '#f0c8a0' };
    var UNDERCOVER = { coat: '#795548', shades: '#333', skin: '#f0c8a0', badge: '#ffd700' };

    function drawPerson(ctx, x, y, size, colors, frame, facingLeft) {
        var s = size;
        var legOffset = Math.sin(frame * 6) * s * 0.1;

        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) { ctx.scale(-1, 1); }

        // Legs
        ctx.fillStyle = colors.pants || colors.suit || colors.dress || '#333';
        ctx.fillRect(-s * 0.15, s * 0.2, s * 0.12, s * 0.3 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.12, s * 0.3 - legOffset);

        // Body
        ctx.fillStyle = colors.body || colors.shirt || colors.suit || colors.dress || colors.uniform || colors.coat || '#666';
        roundRect(ctx, -s * 0.2, -s * 0.15, s * 0.4, s * 0.4, s * 0.06);

        // Head
        ctx.fillStyle = colors.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.3, s * 0.16, 0, Math.PI * 2);
        ctx.fill();

        // Eyes
        ctx.fillStyle = colors.eye || '#fff';
        ctx.beginPath();
        ctx.arc(-s * 0.06, -s * 0.32, s * 0.04, 0, Math.PI * 2);
        ctx.arc(s * 0.06, -s * 0.32, s * 0.04, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = colors.pupil || '#111';
        ctx.beginPath();
        ctx.arc(-s * 0.05, -s * 0.32, s * 0.02, 0, Math.PI * 2);
        ctx.arc(s * 0.07, -s * 0.32, s * 0.02, 0, Math.PI * 2);
        ctx.fill();

        ctx.restore();
    }

    function roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
        ctx.fill();
    }

    // ── Thief ──

    function drawThief(ctx, x, y, size, frame, facingLeft, state) {
        var s = size;
        var legOffset = Math.sin(frame * 6) * s * 0.1;
        var alpha = (state === 'disguise') ? 0.4 : 1;

        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.translate(x, y);
        if (facingLeft) { ctx.scale(-1, 1); }

        // Sprint trail
        if (state === 'sprint') {
            ctx.globalAlpha = 0.15;
            ctx.fillStyle = THIEF.hood;
            roundRect(ctx, -s * 0.2 - s * 0.15, -s * 0.15, s * 0.35, s * 0.4, s * 0.05);
            ctx.globalAlpha = alpha;
        }

        // Legs
        ctx.fillStyle = THIEF.pants;
        ctx.fillRect(-s * 0.15, s * 0.2, s * 0.12, s * 0.28 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.12, s * 0.28 - legOffset);

        // Body
        ctx.fillStyle = THIEF.body;
        roundRect(ctx, -s * 0.22, -s * 0.15, s * 0.44, s * 0.4, s * 0.06);

        // Reaching arm
        if (state === 'stealing') {
            ctx.fillStyle = THIEF.body;
            ctx.fillRect(s * 0.2, -s * 0.05, s * 0.3, s * 0.1);
            ctx.fillStyle = THIEF.skin;
            ctx.beginPath();
            ctx.arc(s * 0.52, 0, s * 0.07, 0, Math.PI * 2);
            ctx.fill();
        }

        // Hood + head
        ctx.fillStyle = THIEF.hood;
        ctx.beginPath();
        ctx.arc(0, -s * 0.3, s * 0.2, 0, Math.PI * 2);
        ctx.fill();

        // Mask
        ctx.fillStyle = THIEF.mask;
        roundRect(ctx, -s * 0.14, -s * 0.28, s * 0.28, s * 0.1, s * 0.03);

        // Eyes (visible through mask)
        ctx.fillStyle = '#fff';
        ctx.beginPath();
        ctx.arc(-s * 0.06, -s * 0.3, s * 0.04, 0, Math.PI * 2);
        ctx.arc(s * 0.06, -s * 0.3, s * 0.04, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#111';
        ctx.beginPath();
        ctx.arc(-s * 0.05, -s * 0.3, s * 0.02, 0, Math.PI * 2);
        ctx.arc(s * 0.07, -s * 0.3, s * 0.02, 0, Math.PI * 2);
        ctx.fill();

        // Hands-up for disguise
        if (state === 'disguise') {
            ctx.fillStyle = THIEF.skin;
            ctx.beginPath();
            ctx.arc(-s * 0.3, -s * 0.45, s * 0.07, 0, Math.PI * 2);
            ctx.arc(s * 0.3, -s * 0.45, s * 0.07, 0, Math.PI * 2);
            ctx.fill();
        }

        ctx.restore();
    }

    // ── NPC: Tourist ──

    function drawTourist(ctx, x, y, size, frame, facingLeft) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legOffset = Math.sin(frame * 4) * s * 0.08;

        // Legs
        ctx.fillStyle = TOURIST.shorts;
        ctx.fillRect(-s * 0.14, s * 0.2, s * 0.11, s * 0.25 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.11, s * 0.25 - legOffset);

        // Body (flower shirt)
        ctx.fillStyle = TOURIST.shirt;
        roundRect(ctx, -s * 0.2, -s * 0.15, s * 0.4, s * 0.4, s * 0.06);
        // Flower dots
        ctx.fillStyle = '#e57373';
        for (var i = 0; i < 4; i++) {
            ctx.beginPath();
            ctx.arc(-s * 0.1 + i * s * 0.07, -s * 0.02 + (i % 2) * s * 0.1, s * 0.025, 0, Math.PI * 2);
            ctx.fill();
        }

        // Head
        ctx.fillStyle = TOURIST.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.3, s * 0.16, 0, Math.PI * 2);
        ctx.fill();

        // Hat
        ctx.fillStyle = TOURIST.hat;
        ctx.fillRect(-s * 0.22, -s * 0.46, s * 0.44, s * 0.08);
        roundRect(ctx, -s * 0.14, -s * 0.52, s * 0.28, s * 0.14, s * 0.04);

        // Eyes
        ctx.fillStyle = '#111';
        ctx.beginPath();
        ctx.arc(-s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.arc(s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.fill();

        // Camera strap
        ctx.strokeStyle = TOURIST.camera;
        ctx.lineWidth = s * 0.03;
        ctx.beginPath();
        ctx.moveTo(-s * 0.1, -s * 0.12);
        ctx.lineTo(s * 0.05, s * 0.1);
        ctx.stroke();
        ctx.fillStyle = TOURIST.camera;
        ctx.fillRect(s * 0.0, s * 0.05, s * 0.12, s * 0.08);

        ctx.restore();
    }

    // ── NPC: Merchant ──

    function drawMerchant(ctx, x, y, size, frame, facingLeft) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legOffset = Math.sin(frame * 4) * s * 0.06;

        // Legs
        ctx.fillStyle = '#444';
        ctx.fillRect(-s * 0.14, s * 0.2, s * 0.11, s * 0.28 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.11, s * 0.28 - legOffset);

        // Body (suit)
        ctx.fillStyle = MERCHANT.suit;
        roundRect(ctx, -s * 0.2, -s * 0.15, s * 0.4, s * 0.4, s * 0.06);
        // Tie
        ctx.fillStyle = MERCHANT.tie;
        ctx.beginPath();
        ctx.moveTo(0, -s * 0.12);
        ctx.lineTo(-s * 0.04, s * 0.15);
        ctx.lineTo(s * 0.04, s * 0.15);
        ctx.closePath();
        ctx.fill();

        // Head
        ctx.fillStyle = MERCHANT.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.3, s * 0.16, 0, Math.PI * 2);
        ctx.fill();

        // Eyes
        ctx.fillStyle = '#111';
        ctx.beginPath();
        ctx.arc(-s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.arc(s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.fill();

        // Briefcase
        ctx.fillStyle = MERCHANT.case_;
        roundRect(ctx, s * 0.18, s * 0.05, s * 0.16, s * 0.12, s * 0.02);
        ctx.fillStyle = '#ffd700';
        ctx.fillRect(s * 0.24, s * 0.1, s * 0.04, s * 0.02);

        ctx.restore();
    }

    // ── NPC: Lady ──

    function drawLady(ctx, x, y, size, frame, facingLeft) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legOffset = Math.sin(frame * 4) * s * 0.05;

        // Dress (covers legs)
        ctx.fillStyle = LADY.dress;
        ctx.beginPath();
        ctx.moveTo(-s * 0.2, -s * 0.15);
        ctx.lineTo(-s * 0.25, s * 0.48 + legOffset);
        ctx.lineTo(s * 0.25, s * 0.48 - legOffset);
        ctx.lineTo(s * 0.2, -s * 0.15);
        ctx.closePath();
        ctx.fill();

        // Body top
        ctx.fillStyle = LADY.dress;
        roundRect(ctx, -s * 0.18, -s * 0.15, s * 0.36, s * 0.3, s * 0.05);

        // Head
        ctx.fillStyle = LADY.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.3, s * 0.16, 0, Math.PI * 2);
        ctx.fill();

        // Hair
        ctx.fillStyle = LADY.hair;
        ctx.beginPath();
        ctx.arc(0, -s * 0.36, s * 0.16, Math.PI, 0);
        ctx.fill();

        // Pearls
        ctx.fillStyle = LADY.pearls;
        for (var i = 0; i < 5; i++) {
            ctx.beginPath();
            ctx.arc(-s * 0.08 + i * s * 0.04, -s * 0.14, s * 0.015, 0, Math.PI * 2);
            ctx.fill();
        }

        // Eyes
        ctx.fillStyle = '#111';
        ctx.beginPath();
        ctx.arc(-s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.arc(s * 0.06, -s * 0.3, s * 0.025, 0, Math.PI * 2);
        ctx.fill();

        // Handbag
        ctx.fillStyle = LADY.bag;
        roundRect(ctx, s * 0.16, -s * 0.02, s * 0.14, s * 0.1, s * 0.03);
        ctx.strokeStyle = LADY.bag;
        ctx.lineWidth = s * 0.02;
        ctx.beginPath();
        ctx.arc(s * 0.23, -s * 0.06, s * 0.06, 0, Math.PI, true);
        ctx.stroke();

        ctx.restore();
    }

    // ── NPC: Boss ──

    function drawBoss(ctx, x, y, size, frame, facingLeft) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legOffset = Math.sin(frame * 3) * s * 0.05;

        // Legs
        ctx.fillStyle = '#111';
        ctx.fillRect(-s * 0.14, s * 0.2, s * 0.11, s * 0.28 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.11, s * 0.28 - legOffset);

        // Coat
        ctx.fillStyle = BOSS.coat;
        roundRect(ctx, -s * 0.24, -s * 0.18, s * 0.48, s * 0.45, s * 0.06);
        // White shirt underneath
        ctx.fillStyle = BOSS.shirt;
        ctx.fillRect(-s * 0.08, -s * 0.12, s * 0.16, s * 0.2);

        // Head
        ctx.fillStyle = BOSS.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.32, s * 0.17, 0, Math.PI * 2);
        ctx.fill();

        // Sunglasses
        ctx.fillStyle = BOSS.shades;
        roundRect(ctx, -s * 0.14, -s * 0.37, s * 0.11, s * 0.07, s * 0.02);
        roundRect(ctx, s * 0.03, -s * 0.37, s * 0.11, s * 0.07, s * 0.02);
        ctx.fillRect(-s * 0.03, -s * 0.34, s * 0.06, s * 0.02);

        // Gold watch
        ctx.fillStyle = BOSS.watch;
        ctx.beginPath();
        ctx.arc(s * 0.22, s * 0.08, s * 0.04, 0, Math.PI * 2);
        ctx.fill();

        ctx.restore();
    }

    // ── Guard: Security ──

    function drawSecurity(ctx, x, y, size, frame, facingLeft, isChasing) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legSpeed = isChasing ? 8 : 4;
        var legOffset = Math.sin(frame * legSpeed) * s * 0.1;

        // Legs
        ctx.fillStyle = SECURITY.uniform;
        ctx.fillRect(-s * 0.15, s * 0.2, s * 0.12, s * 0.28 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.12, s * 0.28 - legOffset);

        // Body
        ctx.fillStyle = SECURITY.uniform;
        roundRect(ctx, -s * 0.22, -s * 0.18, s * 0.44, s * 0.42, s * 0.06);

        // Badge
        ctx.fillStyle = SECURITY.badge;
        ctx.beginPath();
        ctx.moveTo(s * 0.08, -s * 0.08);
        ctx.lineTo(s * 0.14, -s * 0.02);
        ctx.lineTo(s * 0.08, s * 0.04);
        ctx.lineTo(s * 0.02, -s * 0.02);
        ctx.closePath();
        ctx.fill();

        // Head
        ctx.fillStyle = SECURITY.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.32, s * 0.17, 0, Math.PI * 2);
        ctx.fill();

        // Hat
        ctx.fillStyle = SECURITY.hat;
        ctx.fillRect(-s * 0.2, -s * 0.48, s * 0.4, s * 0.1);
        roundRect(ctx, -s * 0.15, -s * 0.54, s * 0.3, s * 0.14, s * 0.04);
        // Hat brim shine
        ctx.fillStyle = '#1565c0';
        ctx.fillRect(-s * 0.12, -s * 0.48, s * 0.24, s * 0.03);

        // Stern eyes
        ctx.fillStyle = '#111';
        ctx.fillRect(-s * 0.1, -s * 0.34, s * 0.07, s * 0.03);
        ctx.fillRect(s * 0.03, -s * 0.34, s * 0.07, s * 0.03);

        // Exclamation when chasing
        if (isChasing) {
            ctx.fillStyle = '#f44336';
            ctx.font = 'bold ' + (s * 0.3) + 'px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('!', 0, -s * 0.65);
        }

        ctx.restore();
    }

    // ── Guard: Undercover ──

    function drawUndercover(ctx, x, y, size, frame, facingLeft, isChasing, isRevealed) {
        var s = size;
        ctx.save();
        ctx.translate(x, y);
        if (facingLeft) ctx.scale(-1, 1);

        var legSpeed = isChasing ? 8 : 4;
        var legOffset = Math.sin(frame * legSpeed) * s * 0.08;

        // Legs
        ctx.fillStyle = '#555';
        ctx.fillRect(-s * 0.14, s * 0.2, s * 0.11, s * 0.28 + legOffset);
        ctx.fillRect(s * 0.03, s * 0.2, s * 0.11, s * 0.28 - legOffset);

        // Trench coat
        ctx.fillStyle = UNDERCOVER.coat;
        roundRect(ctx, -s * 0.22, -s * 0.18, s * 0.44, s * 0.44, s * 0.06);

        // Head
        ctx.fillStyle = UNDERCOVER.skin;
        ctx.beginPath();
        ctx.arc(0, -s * 0.32, s * 0.17, 0, Math.PI * 2);
        ctx.fill();

        // Sunglasses
        ctx.fillStyle = UNDERCOVER.shades;
        roundRect(ctx, -s * 0.13, -s * 0.37, s * 0.1, s * 0.06, s * 0.02);
        roundRect(ctx, s * 0.03, -s * 0.37, s * 0.1, s * 0.06, s * 0.02);
        ctx.fillRect(-s * 0.03, -s * 0.35, s * 0.06, s * 0.02);

        // Badge reveal
        if (isRevealed) {
            ctx.fillStyle = UNDERCOVER.badge;
            ctx.beginPath();
            ctx.moveTo(s * 0.1, -s * 0.08);
            ctx.lineTo(s * 0.16, -s * 0.02);
            ctx.lineTo(s * 0.1, s * 0.04);
            ctx.lineTo(s * 0.04, -s * 0.02);
            ctx.closePath();
            ctx.fill();
        }

        if (isChasing) {
            ctx.fillStyle = '#f44336';
            ctx.font = 'bold ' + (s * 0.3) + 'px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('!', 0, -s * 0.65);
        }

        ctx.restore();
    }

    // ── Loot Icons ──

    function drawLoot(ctx, x, y, size, type) {
        ctx.save();
        ctx.font = size + 'px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        var emoji = { tourist: '\uD83D\uDCB0', merchant: '\uD83D\uDC8E', lady: '\uD83D\uDC8D', boss: '\uD83D\uDCB3' };
        ctx.fillText(emoji[type] || '\uD83D\uDCB0', x, y);
        ctx.restore();
    }

    // ── Scenery ──

    function drawTree(ctx, x, y, size) {
        var s = size;
        ctx.fillStyle = '#5d4037';
        ctx.fillRect(x - s * 0.05, y, s * 0.1, s * 0.3);
        ctx.fillStyle = '#2e7d32';
        ctx.beginPath();
        ctx.arc(x, y - s * 0.05, s * 0.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#388e3c';
        ctx.beginPath();
        ctx.arc(x - s * 0.08, y + s * 0.02, s * 0.14, 0, Math.PI * 2);
        ctx.fill();
    }

    function drawLamp(ctx, x, y, size) {
        var s = size;
        ctx.fillStyle = '#616161';
        ctx.fillRect(x - s * 0.03, y - s * 0.1, s * 0.06, s * 0.5);
        ctx.fillStyle = '#ffeb3b';
        ctx.globalAlpha = 0.6;
        ctx.beginPath();
        ctx.arc(x, y - s * 0.15, s * 0.08, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
    }

    function drawBench(ctx, x, y, size) {
        var s = size;
        ctx.fillStyle = '#8d6e63';
        ctx.fillRect(x - s * 0.2, y, s * 0.4, s * 0.06);
        ctx.fillRect(x - s * 0.18, y + s * 0.06, s * 0.04, s * 0.1);
        ctx.fillRect(x + s * 0.14, y + s * 0.06, s * 0.04, s * 0.1);
    }

    // ── UI Elements ──

    function drawProgressRing(ctx, x, y, radius, progress, color) {
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.2)';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.stroke();

        ctx.strokeStyle = color || '#4caf50';
        ctx.lineWidth = 3;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.arc(x, y, radius, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * progress);
        ctx.stroke();
        ctx.restore();
    }

    function drawAlertBar(ctx, x, y, width, value, maxValue) {
        var ratio = value / maxValue;
        var color = ratio < 0.3 ? '#4caf50' : ratio < 0.7 ? '#ff9800' : '#f44336';
        ctx.fillStyle = 'rgba(0,0,0,0.4)';
        roundRect(ctx, x - width / 2, y, width, 4, 2);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.rect(x - width / 2, y, width * ratio, 4);
        ctx.fill();
    }

    // Public API
    return {
        drawThief: drawThief,
        drawTourist: drawTourist,
        drawMerchant: drawMerchant,
        drawLady: drawLady,
        drawBoss: drawBoss,
        drawSecurity: drawSecurity,
        drawUndercover: drawUndercover,
        drawLoot: drawLoot,
        drawTree: drawTree,
        drawLamp: drawLamp,
        drawBench: drawBench,
        drawProgressRing: drawProgressRing,
        drawAlertBar: drawAlertBar,
        roundRect: roundRect
    };
})();
