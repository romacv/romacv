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

const IMAGE_VERSION = '20261117';

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

function byMyAppThenPeriod(a, b) {
    const first = a.project.myApp;
    const second = b.project.myApp;
    if (first && second) return first - second;
    if (first || second) return first ? -1 : 1;
    return byShownPeriodDesc(a, b);
}

function renderGroup(container, id, heading, entries) {
    if (!entries.length) return;

    const group = createElement('div', 'work-group');
    group.id = id;
    const title = createElement('h4', 'group-title', heading);
    group.appendChild(title);

    const grid = createElement('ul', 'project-grid');
    grid.id = `grid-${id}`;
    entries.forEach(entry => grid.appendChild(renderCard(entry)));
    group.appendChild(grid);

    container.appendChild(group);
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

const pageIndexLinks = new Map();
const visibleSections = new Set();
let sectionObserver;

function sectionId(element) {
    return element.id === 'experience-list' ? 'experience' : element.id;
}

function markCurrentSection() {
    const visibleIds = new Set([...visibleSections].map(sectionId));
    const current = [...pageIndexLinks.keys()].find(id => visibleIds.has(id));
    if (!current) return;
    pageIndexLinks.forEach((link, id) => {
        if (id === current) {
            link.setAttribute('aria-current', 'true');
        } else {
            link.removeAttribute('aria-current');
        }
    });
}

function observeSections() {
    if (!sectionObserver) return;
    sectionObserver.disconnect();
    visibleSections.clear();
    [...pageIndexLinks.keys(), 'experience-list'].forEach(id => {
        const section = document.getElementById(id);
        if (section) sectionObserver.observe(section);
    });
}

function setupPageIndex() {
    document.querySelectorAll('.page-index a[href^="#"]').forEach(link => {
        pageIndexLinks.set(link.getAttribute('href').substring(1), link);
    });
    if (!pageIndexLinks.size || !('IntersectionObserver' in window)) return;

    sectionObserver = new IntersectionObserver(observed => {
        observed.forEach(({ target, isIntersecting }) => {
            if (isIntersecting) {
                visibleSections.add(target);
            } else {
                visibleSections.delete(target);
            }
        });
        markCurrentSection();
    }, { rootMargin: '-64px 0px -50% 0px' });
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
        renderGroup(container, 'apps', 'iOS / macOS apps', shown.filter(entry => entry.project.type !== 'tool').sort(byMyAppThenPeriod));
        renderGroup(container, 'tools', 'Tools and bots', shown.filter(entry => entry.project.type === 'tool').sort(byShownPeriodDesc));
        observeSections();

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

    setupPageIndex();
    render();
}

// Check for hash in URL and redirect if needed
function checkHashAndRedirect(data) {
    const hash = window.location.hash.substring(1);
    if (hash && Object.values(data).some(year => hash in year)) {
        window.location.href = `project?name=${hash}`;
    }
}

async function init() {
    const data = await loadData();
    if (data) {
        renderProjects(data);
        checkHashAndRedirect(data);
    }
}

// Run when the DOM is loaded
document.addEventListener('DOMContentLoaded', init); 