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

        projectCol.appendChild(projectLink);
        projectsRow.appendChild(projectCol);
    });

    yearBlock.appendChild(projectsRow);
    container.appendChild(yearBlock);
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

    const apps = years
        .flatMap(year => Object.entries(data[year]))
        .filter(([, project]) => project.myApp)
        .sort((a, b) => a[1].myApp - b[1].myApp);
    renderBlock(container, 'My apps', apps);

    years.forEach(year => {
        renderBlock(container, year, Object.entries(data[year]).filter(([, project]) => !project.myApp));
    });
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