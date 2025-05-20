// Load the JSON data
async function loadData() {
    try {
        const response = await fetch('data.json');
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('Error loading data:', error);
        return null;
    }
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

    years.forEach(year => {
        const yearBlock = document.createElement('div');
        yearBlock.className = 'year-block';
        
        const yearHeader = document.createElement('h3');
        yearHeader.textContent = year;
        yearBlock.appendChild(yearHeader);

        const projectsRow = document.createElement('div');
        projectsRow.className = 'row';

        Object.entries(data[year]).forEach(([key, project]) => {
            const projectCol = document.createElement('div');
            projectCol.className = 'col-md-4 col-sm-6 col-12';

            const projectLink = document.createElement('a');
            projectLink.href = `/project/${key}`;
            projectLink.style.backgroundImage = `url('img/works/${project.img}.jpg')`;
            projectLink.setAttribute('data-name', key);

            projectCol.appendChild(projectLink);
            projectsRow.appendChild(projectCol);
        });

        yearBlock.appendChild(projectsRow);
        container.appendChild(yearBlock);
    });
}

// Initialize the page
async function init() {
    const data = await loadData();
    if (data) {
        renderProjects(data);
    }
}

// Run when the DOM is loaded
document.addEventListener('DOMContentLoaded', init); 