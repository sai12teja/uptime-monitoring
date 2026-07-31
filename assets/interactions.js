// Modal keyboard behavior (§13.5/§13.6): focus trap + Escape-to-close +
// return focus to whatever triggered it. Shared by the detail slide-over
// and the Add Monitor modal.
//
// Dash is a React SPA — assets/*.js runs at DOMContentLoaded, which fires
// BEFORE React has mounted the app's component tree into the page. Looking
// up a wrapper element once at that point finds nothing. Instead we observe
// document.body itself (always present) with subtree:true, and re-query the
// wrapper fresh on every mutation — this catches both the element's eventual
// first mount AND every open/close re-render after that.
document.addEventListener('DOMContentLoaded', function () {
    function getFocusable(container) {
        return Array.prototype.slice
            .call(container.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            ))
            .filter(function (el) { return !el.disabled && el.offsetParent !== null; });
    }

    function setupModal(wrapperId, openClass, panelSelector, closeBtnId) {
        var lastFocused = null;

        var observer = new MutationObserver(function () {
            var wrapper = document.getElementById(wrapperId);
            if (!wrapper) return;

            var panel = wrapper.querySelector(panelSelector);
            var isOpen = wrapper.classList.contains(openClass);

            if (isOpen && panel) {
                if (document.activeElement !== panel && !panel.contains(document.activeElement)) {
                    lastFocused = document.activeElement;
                    panel.focus();
                }
            } else if (!isOpen && lastFocused) {
                lastFocused.focus();
                lastFocused = null;
            }
        });
        observer.observe(document.body, { attributes: true, attributeFilter: ['class'], subtree: true, childList: true });

        document.addEventListener('keydown', function (e) {
            var wrapper = document.getElementById(wrapperId);
            if (!wrapper || !wrapper.classList.contains(openClass)) return;

            if (e.key === 'Escape') {
                var closeBtn = document.getElementById(closeBtnId);
                if (closeBtn) closeBtn.click();
                return;
            }

            if (e.key === 'Tab') {
                var panel = wrapper.querySelector(panelSelector);
                if (!panel) return;
                var focusables = getFocusable(panel);
                if (!focusables.length) return;
                var first = focusables[0];
                var last = focusables[focusables.length - 1];

                if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        });
    }

    setupModal('detail-wrapper', 'detail-open', '.detail-panel', 'detail-close');
    setupModal('add-monitor-wrapper', 'addmonitor-open', '.add-monitor-modal', 'add-monitor-close');
});
