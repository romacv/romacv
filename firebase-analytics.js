import { initializeApp } from "https://www.gstatic.com/firebasejs/11.2.0/firebase-app.js";
import { getAnalytics, logEvent } from "https://www.gstatic.com/firebasejs/11.2.0/firebase-analytics.js";

const firebaseConfig = {
    apiKey: "AIzaSyBtK4k81aqrkGHv6k3EAf3oHriZl-nZjRM",
    authDomain: "resromcom-493b4.firebaseapp.com",
    projectId: "resromcom-493b4",
    storageBucket: "resromcom-493b4.firebasestorage.app",
    messagingSenderId: "119380595463",
    appId: "1:119380595463:web:e0d4abda12b94a4d0aaba2",
    measurementId: "G-YJH2612HQ3"
};

const app = initializeApp(firebaseConfig);
const analytics = getAnalytics(app);

document.addEventListener('click', function(e) {
    const link = e.target.closest('a');
    if (link) {
        logEvent(analytics, 'link_click', {
            link_url: link.href,
            link_text: link.textContent.trim() || link.getAttribute('data-name') || 'image_link',
            page: window.location.pathname
        });
    }
});
