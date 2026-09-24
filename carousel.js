// "What I build" carousel: keep the page dots in sync with the centered card
function initBuildCardsCarousel() {
    const scroller = document.querySelector('.build-cards');
    const dotsWrap = document.querySelector('.build-cards-dots');
    const prevButton = document.querySelector('.build-cards-prev');
    const nextButton = document.querySelector('.build-cards-next');
    if (!scroller || !dotsWrap) return;

    const cards = Array.from(scroller.children);
    const dots = Array.from(dotsWrap.children);
    if (!cards.length || cards.length !== dots.length) return;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let activeIndex = 0;

    function setActive(index) {
        activeIndex = index;
        dots.forEach((dot, i) => {
            const isActive = i === index;
            dot.classList.toggle('active', isActive);
            if (isActive) {
                dot.setAttribute('aria-current', 'true');
            } else {
                dot.removeAttribute('aria-current');
            }
        });
        if (prevButton) prevButton.disabled = index === 0;
        if (nextButton) nextButton.disabled = index === cards.length - 1;
    }

    function goTo(index) {
        const clamped = Math.max(0, Math.min(cards.length - 1, index));
        const card = cards[clamped];
        scroller.scrollTo({
            left: card.offsetLeft - (scroller.clientWidth - card.offsetWidth) / 2,
            behavior: reduceMotion ? 'auto' : 'smooth'
        });
    }

    dots.forEach((dot, i) => {
        dot.addEventListener('click', () => goTo(i));
    });

    if (prevButton) {
        prevButton.addEventListener('click', () => goTo(activeIndex - 1));
    }
    if (nextButton) {
        nextButton.addEventListener('click', () => goTo(activeIndex + 1));
    }

    // Nearest-center math on scroll, throttled with rAF; also runs on load and resize
    let ticking = false;
    function updateActiveByScroll() {
        const scrollerRect = scroller.getBoundingClientRect();
        const scrollerCenter = scrollerRect.left + scrollerRect.width / 2;
        let closestIndex = 0;
        let closestDistance = Infinity;
        cards.forEach((card, i) => {
            const rect = card.getBoundingClientRect();
            const cardCenter = rect.left + rect.width / 2;
            const distance = Math.abs(cardCenter - scrollerCenter);
            if (distance < closestDistance) {
                closestDistance = distance;
                closestIndex = i;
            }
        });
        setActive(closestIndex);
        ticking = false;
    }
    function scheduleUpdate() {
        if (!ticking) {
            window.requestAnimationFrame(updateActiveByScroll);
            ticking = true;
        }
    }
    scroller.addEventListener('scroll', scheduleUpdate);
    window.addEventListener('resize', scheduleUpdate);
    updateActiveByScroll();
}

document.addEventListener('DOMContentLoaded', initBuildCardsCarousel);
