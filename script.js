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

function renderBlock(container, heading, entries) {
    if (!entries.length) return;

    const yearBlock = document.createElement('div');
    yearBlock.className = 'year-block';

    const yearHeader = document.createElement('h3');
    yearHeader.textContent = heading;
    yearBlock.appendChild(yearHeader);

    const projectsRow = document.createElement('div');
    projectsRow.className = 'row';

    entries.forEach(([key, project]) => {
        const projectCol = document.createElement('div');
        projectCol.className = 'col-md-4 col-sm-6 col-12';

        const projectLink = document.createElement('a');
        projectLink.href = project.page || `project?name=${key}`;
        const projectImage = document.createElement('img');
        projectImage.src = `img/works/${project.cover || `${project.img}.jpg`}?v=20261013b`;
        projectImage.alt = project.title;
        projectImage.loading = 'lazy';
        projectLink.appendChild(projectImage);
        projectLink.setAttribute('data-name', project.title);

        if (KIND_TAGS[project.kind]) {
            const kindTag = document.createElement('span');
            kindTag.className = 'kind-tag';
            kindTag.textContent = KIND_TAGS[project.kind];
            projectLink.appendChild(kindTag);
        }

        projectCol.appendChild(projectLink);
        projectsRow.appendChild(projectCol);
    });

    yearBlock.appendChild(projectsRow);
    container.appendChild(yearBlock);
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

    // Sort years in reverse chronological order
    const years = Object.keys(data).sort((a, b) => {
        const yearA = a.split(' - ')[0];
        const yearB = b.split(' - ')[0];
        return parseInt(yearB) - parseInt(yearA);
    });

    const kinds = new Set(years.flatMap(year => Object.values(data[year]).map(project => project.kind)));
    const filters = document.createElement('div');
    filters.className = 'kind-filters';
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', 'Filter projects by kind');
    container.parentNode.insertBefore(filters, container);

    const requestedKind = new URLSearchParams(window.location.search).get('kind');
    let activeKind = kinds.has(requestedKind) && KIND_FILTERS[requestedKind] ? requestedKind : '';

    const matches = project => !activeKind || project.kind === activeKind;

    function render() {
        container.replaceChildren();

        const apps = years
            .flatMap(year => Object.entries(data[year]))
            .filter(([, project]) => project.myApp && matches(project))
            .sort((a, b) => a[1].myApp - b[1].myApp);
        renderBlock(container, 'My apps', apps);

        years.forEach(year => {
            renderBlock(container, year, Object.entries(data[year]).filter(([, project]) => !project.myApp && matches(project)));
        });

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
    const data = await loadData();
    if (data) {
        renderProjects(data);
        checkHashAndRedirect();
    }
}

// Run when the DOM is loaded
document.addEventListener('DOMContentLoaded', init); 