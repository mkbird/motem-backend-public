/* input.js — Native Bridge for body pose data */

(function () {
    'use strict';

    // Default input state
    var defaultInput = {
        bodyLeanX: 0,       // -1 ~ 1, left/right lean
        bodyLeanY: 0,       // -1 ~ 1, forward/back lean
        isReachingOut: false,
        isDucking: false,
        isHandsUp: false,
        bodyDetected: false
    };

    window._gameInput = Object.assign({}, defaultInput);

    /**
     * Called from Native (iOS WKWebView) every frame.
     * @param {Object} data - body pose data
     */
    window.updateInput = function (data) {
        window._gameInput = data;
    };

    /**
     * Get current input snapshot for game loop.
     * @returns {Object}
     */
    window.getInput = function () {
        return window._gameInput;
    };
})();
