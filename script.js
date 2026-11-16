// Load the JSON data
async function loadData() {
    try {
        const response = await fetch('data.json', { cache: 'no-cache' });
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('Error loading data:', error);
        return null;
    }
}

const KIND_TAGS = {
    own: 'Own project',
    freelance: 'Freelance',
    fulltime: 'Full-time job'
};

const KIND_FILTERS = {
    own: 'Own projects',
    freelance: 'Freelance',
    fulltime: 'Full-time jobs'
};

const IMAGE_VERSION = '20261116';

function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}

function firstSentence(text) {
    const flat = text.replace(/\s+/g, ' ').trim();
    const end = flat.indexOf('. ');
    return end === -1 ? flat : flat.slice(0, end + 1);
}

function entryYear(year) {
    return parseInt(year.split(' - ')[0], 10);
}

function entryHref(key, project) {
    return project.page || `project?name=${key}`;
}

function entryImage(project) {
    return project.cover
        ? `img/works/${project.cover}?v=${IMAGE_VERSION}`
        : `img/works/thumbs/${project.img}.jpg?v=${IMAGE_VERSION}`;
}

function shownPeriod(entry) {
    const found = (entry.project.years || '').match(/\d{4}/g);
    const start = found ? parseInt(found[0], 10) : entryYear(entry.year);
    const end = found ? parseInt(found[found.length - 1], 10) : entryYear(entry.year);
    return { start, end };
}

function byShownPeriodDesc(a, b) {
    const first = shownPeriod(a);
    const second = shownPeriod(b);
    return second.end - first.end || second.start - first.start;
}

function renderCard({ key, year, project }) {
    const item = document.createElement('li');

    const link = createElement('a', 'project-card');
    link.href = entryHref(key, project);

    const thumb = createElement('span', 'thumb');
    const image = document.createElement('img');
    image.src = entryImage(project);
    image.alt = '';
    image.width = 960;
    image.height = 408;
    image.loading = 'lazy';
    image.decoding = 'async';
    thumb.appendChild(image);
    link.appendChild(thumb);

    link.appendChild(createElement('h5', 'title', project.title));

    const meta = [project.years || year, KIND_TAGS[project.kind], project.platforms].filter(Boolean).join(' \u00b7 ');
    link.appendChild(createElement('span', 'meta', meta));
    link.appendChild(createElement('span', 'summary', firstSentence(project.desc)));

    item.appendChild(link);
    return item;
}

const COLLAPSED_ROLES = 4;
const COLLAPSED_CARDS = 6;

function attachToggle(target, expandLabel, scrollTarget) {
    const button = createElement('button', 'toggle');
    button.type = 'button';
    button.setAttribute('aria-controls', target.id);

    function sync() {
        const collapsed = target.classList.contains('is-collapsed');
        button.setAttribute('aria-expanded', String(!collapsed));
        button.textContent = collapsed ? expandLabel : 'Show fewer';
    }

    button.addEventListener('click', () => {
        const collapsed = target.classList.toggle('is-collapsed');
        sync();
        if (collapsed && scrollTarget.getBoundingClientRect().top < 0) {
            const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            scrollTarget.scrollIntoView({ block: 'start', behavior: reduceMotion ? 'auto' : 'smooth' });
        }
    });

    target.classList.add('is-collapsed');
    sync();
    target.after(button);
}

function setupExperienceToggle() {
    const list = document.getElementById('experience-list');
    if (!list || list.children.length <= COLLAPSED_ROLES) return;
    const title = list.previousElementSibling || list;
    attachToggle(list, `Show all ${list.children.length} roles`, title);
}

function renderGroup(container, id, heading, noun, entries) {
    if (!entries.length) return;

    const group = createElement('div', 'work-group');
    const title = createElement('h4', 'group-title', heading);
    group.appendChild(title);

    const grid = createElement('ul', 'project-grid');
    grid.id = `grid-${id}`;
    entries.forEach(entry => grid.appendChild(renderCard(entry)));
    group.appendChild(grid);
    if (entries.length > COLLAPSED_CARDS) {
        attachToggle(grid, `Show all ${entries.length} ${noun}`, title);
    }

    container.appendChild(group);
}

function renderAlongside(entries) {
    const element = document.getElementById('experience-alongside');
    if (!element) return;

    const own = entries.filter(entry => entry.project.kind === 'own');
    const freelance = entries.filter(entry => entry.project.kind === 'freelance');
    if (!own.length || !freelance.length) return;

    const apps = entries.filter(entry => entry.project.myApp).length;
    const ownFrom = Math.min(...own.map(entry => entryYear(entry.year)));
    const freelanceYears = freelance.map(entry => entryYear(entry.year));
    element.textContent = `Alongside: ${own.length} apps and tools of my own since ${ownFrom}, ${apps} of them on the App Store, and ${freelance.length} freelance apps for clients from ${Math.min(...freelanceYears)} to ${Math.max(...freelanceYears)}.`;
}

function renderFilters(filters, kinds, onSelect) {
    [['', 'All'], ...Object.entries(KIND_FILTERS).filter(([kind]) => kinds.has(kind))].forEach(([kind, label]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.dataset.kind = kind;
        button.addEventListener('click', () => onSelect(kind));
        filters.appendChild(button);
    });
}

// Function to render the projects
function renderProjects(data) {
    const container = document.getElementById('projects-container');
    if (!container) return;

    // Years in reverse chronological order; entries keep their order inside a year
    const years = Object.keys(data).sort((a, b) => entryYear(b) - entryYear(a));
    const entries = years.flatMap(year =>
        Object.entries(data[year]).map(([key, project]) => ({ key, year, project }))
    );

    renderAlongside(entries);

    const kinds = new Set(entries.map(entry => entry.project.kind));
    const filters = document.createElement('div');
    filters.className = 'kind-filters';
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', 'Filter projects by kind');
    container.parentNode.insertBefore(filters, container);

    const requestedKind = new URLSearchParams(window.location.search).get('kind');
    let activeKind = kinds.has(requestedKind) && KIND_FILTERS[requestedKind] ? requestedKind : '';

    const matches = entry => !activeKind || entry.project.kind === activeKind;

    function render() {
        container.replaceChildren();

        const shown = entries.filter(matches);
        renderGroup(container, 'my-apps', 'My apps', 'apps', shown.filter(entry => entry.project.myApp).sort((a, b) => a.project.myApp - b.project.myApp));
        renderGroup(container, 'other-projects', 'Other projects', 'projects', shown.filter(entry => !entry.project.myApp).sort(byShownPeriodDesc));

        filters.querySelectorAll('button').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.kind === activeKind));
        });
    }

    renderFilters(filters, kinds, kind => {
        activeKind = kind;
        const url = new URL(window.location.href);
        if (kind) {
            url.searchParams.set('kind', kind);
        } else {
            url.searchParams.delete('kind');
        }
        history.replaceState(null, '', url);
        render();
    });

    render();
}

// Check for hash in URL and redirect if needed
function checkHashAndRedirect() {
    const hash = window.location.hash.substring(1);
    if (hash) {
        window.location.href = `project?name=${hash}`;
    }
}

async function init() {
    setupExperienceToggle();
    const data = await loadData();
    if (data) {
        renderProjects(data);
        checkHashAndRedirect();
    }
}

// Run when the DOM is loaded
document.addEventListener('DOMContentLoaded', init); 