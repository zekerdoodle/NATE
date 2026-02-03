const chatContainer = document.getElementById('chat-container');
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
const sessionList = document.getElementById('session-list');
const newChatBtn = document.getElementById('new-chat-btn');

let currentSessionId = null;

// Auto-resize textarea
userInput.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
    if (this.value === '') {
        this.style.height = 'auto';
    }
});

// Send message on Enter (but Shift+Enter for new line)
userInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

sendBtn.addEventListener('click', sendMessage);
newChatBtn.addEventListener('click', () => createNewSession());

// --- Session Management ---

async function loadSessions() {
    try {
        const response = await fetch('/api/sessions');
        const sessions = await response.json();
        renderSessionList(sessions);

        if (!currentSessionId) {
            if (sessions.length > 0) {
                loadSession(sessions[0].id);
            } else {
                createNewSession();
            }
        }
    } catch (error) {
        console.error('Failed to load sessions:', error);
    }
}

function renderSessionList(sessions) {
    sessionList.innerHTML = '';
    sessions.forEach(session => {
        const div = document.createElement('div');
        div.className = `session-item ${session.id === currentSessionId ? 'active' : ''}`;
        div.innerHTML = `
            <span class="title">${session.title}</span>
            <button class="delete-btn" onclick="deleteSession(event, '${session.id}')" title="Delete Chat">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" style="width:16px;height:16px;">
                    <path fill-rule="evenodd" d="M8.75 1A2.75 2.75 0 006 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 10.23 1.482l.149-.022.841 10.518A2.75 2.75 0 007.596 19h4.807a2.75 2.75 0 002.742-2.53l.841-10.52.149.023a.75.75 0 00.23-1.482A41.03 41.03 0 0014 4.193V3.75A2.75 2.75 0 0011.25 1h-2.5zM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4zM8.58 7.72a.75.75 0 00-1.5.06l.3 7.5a.75.75 0 101.5-.06l-.3-7.5zm4.34.06a.75.75 0 10-1.5-.06l-.3 7.5a.75.75 0 101.5.06l.3-7.5z" clip-rule="evenodd" />
                </svg>
            </button>
        `;

        // Click handler for session selection
        div.addEventListener('click', (e) => {
            // Ignore if delete button was clicked (handled by onclick)
            if (e.target.closest('.delete-btn')) return;
            loadSession(session.id);
        });

        // Rename handler
        div.addEventListener('dblclick', () => {
            const titleSpan = div.querySelector('.title');
            const currentTitle = titleSpan.textContent;
            const input = document.createElement('input');
            input.value = currentTitle;
            input.style.background = '#343541';
            input.style.color = 'white';
            input.style.border = '1px solid #555';
            input.style.borderRadius = '4px';
            input.style.padding = '2px 4px';
            input.style.width = '100%';

            input.onblur = async () => {
                const newTitle = input.value.trim();
                if (newTitle && newTitle !== currentTitle) {
                    await updateSessionTitle(session.id, newTitle);
                }
                loadSessions();
            };

            input.onkeydown = (e) => {
                if (e.key === 'Enter') input.blur();
            };

            titleSpan.replaceWith(input);
            input.focus();
        });

        sessionList.appendChild(div);
    });
}

async function createNewSession() {
    try {
        const response = await fetch('/api/sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: 'New Chat' })
        });
        const session = await response.json();
        currentSessionId = session.id;
        chatContainer.innerHTML = '';
        loadSessions();
    } catch (error) {
        console.error('Error creating session:', error);
    }
}

async function loadSession(sessionId) {
    if (currentSessionId === sessionId) return;

    currentSessionId = sessionId;
    loadSessions(); // Sync UI active state

    try {
        const response = await fetch(`/api/sessions/${sessionId}`);
        const session = await response.json();

        chatContainer.innerHTML = '';

        session.history.forEach(msg => {
            addMessage(msg.content, msg.role === 'assistant' ? 'nate' : 'user');
        });
    } catch (error) {
        console.error('Error loading session:', error);
    }
}

async function updateSessionTitle(sessionId, newTitle) {
    await fetch(`/api/sessions/${sessionId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle })
    });
}

// Expose to window for onclick
window.deleteSession = async function (event, sessionId) {
    event.stopPropagation();
    console.log("Delete requested for", sessionId);
    if (!confirm('Delete this chat?')) return;

    await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
    if (currentSessionId === sessionId) {
        currentSessionId = null;
        chatContainer.innerHTML = '';
    }
    loadSessions();
}

// --- Chat Logic ---

async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;

    if (!currentSessionId) {
        await createNewSession();
    }

    addMessage(text, 'user');
    userInput.value = '';
    userInput.style.height = 'auto';
    userInput.disabled = true;
    sendBtn.disabled = true;

    const loadingId = addLoadingIndicator();

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,
                session_id: currentSessionId
            })
        });

        if (!response.ok) throw new Error('Network response was not ok');

        const data = await response.json();
        removeMessage(loadingId);
        addMessage(data.response, 'nate');
        loadSessions(); // Update title/order

    } catch (error) {
        console.error('Error:', error);
        removeMessage(loadingId);
        addMessage('Sorry, something went wrong.', 'system');
    } finally {
        userInput.disabled = false;
        sendBtn.disabled = false;
        userInput.focus();
    }
}

function addMessage(text, type) {
    const wrapper = document.createElement('div');
    wrapper.className = `message-wrapper ${type}`;

    const messageDiv = document.createElement('div');
    messageDiv.className = 'message';

    // Avatar
    const avatar = document.createElement('div');
    avatar.className = `avatar ${type}`;
    if (type === 'nate') {
        avatar.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" style="width:20px;height:20px;"><path fill-rule="evenodd" d="M9.315 7.584C12.195 3.883 16.695 1.5 21.75 1.5a.75.75 0 01.75.75c0 5.056-2.383 9.555-6.084 12.436h.001c-3.7 2.881-8.199 5.264-13.254 5.264a.75.75 0 01-.75-.75c0-5.055 2.383-9.554 6.084-12.435zm.82 1.699l-.733-.615c.377-.476.789-.93 1.23-1.356l.615.733c-.384.371-.741.766-1.064 1.177l-.048.061zm2.505-2.505l-.615-.733c.427-.44.881-.852 1.356-1.23l.733.615c-.411.323-.807.68-1.177 1.064l-.061.048zm-3.465 4.63l-.733-.615c.476-.377.93-.789 1.356-1.23l.615.733c-.371.384-.766.741-1.177 1.064l-.061.048zM7.584 9.315l-.615.733c-.44-.427-.852-.881-1.23-1.356l.615-.733c.323.411.68.807 1.064 1.177l-.048.061z" clip-rule="evenodd" /></svg>`;
    } else {
        avatar.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" style="width:20px;height:20px;"><path fill-rule="evenodd" d="M7.5 6a4.5 4.5 0 119 0 4.5 4.5 0 01-9 0zM3.751 20.105a8.25 8.25 0 0116.498 0 .75.75 0 01-.437.695A18.683 18.683 0 0112 22.5c-2.786 0-5.433-.608-7.812-1.7a.75.75 0 01-.437-.695z" clip-rule="evenodd" /></svg>`;
    }

    // Content
    const contentDiv = document.createElement('div');
    contentDiv.className = 'content';

    if (type === 'nate') {
        contentDiv.innerHTML = marked.parse(text);
        const links = contentDiv.querySelectorAll('a');
        links.forEach(link => {
            const href = link.getAttribute('href');
            if (href && !href.startsWith('http') && !href.startsWith('/')) {
                link.setAttribute('href', `/docs/it_docs/${href}`);
                link.setAttribute('target', '_blank');
            }
        });
    } else {
        contentDiv.textContent = text;
    }

    messageDiv.appendChild(avatar);
    messageDiv.appendChild(contentDiv);
    wrapper.appendChild(messageDiv);

    chatContainer.appendChild(wrapper);

    // Scroll to bottom
    const container = document.getElementById('chat-container');
    container.scrollTop = container.scrollHeight;

    return wrapper.id = 'msg-' + Date.now();
}

function addLoadingIndicator() {
    const wrapper = document.createElement('div');
    wrapper.className = 'message-wrapper nate';
    wrapper.id = 'loading-' + Date.now();

    const messageDiv = document.createElement('div');
    messageDiv.className = 'message';

    const avatar = document.createElement('div');
    avatar.className = 'avatar nate';
    avatar.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" style="width:20px;height:20px;"><path fill-rule="evenodd" d="M9.315 7.584C12.195 3.883 16.695 1.5 21.75 1.5a.75.75 0 01.75.75c0 5.056-2.383 9.555-6.084 12.436h.001c-3.7 2.881-8.199 5.264-13.254 5.264a.75.75 0 01-.75-.75c0-5.055 2.383-9.554 6.084-12.435zm.82 1.699l-.733-.615c.377-.476.789-.93 1.23-1.356l.615.733c-.384.371-.741.766-1.064 1.177l-.048.061zm2.505-2.505l-.615-.733c.427-.44.881-.852 1.356-1.23l.733.615c-.411.323-.807.68-1.177 1.064l-.061.048zm-3.465 4.63l-.733-.615c.476-.377.93-.789 1.356-1.23l.615.733c-.371.384-.766.741-1.177 1.064l-.061.048zM7.584 9.315l-.615.733c-.44-.427-.852-.881-1.23-1.356l.615-.733c.323.411.68.807 1.064 1.177l-.048.061z" clip-rule="evenodd" /></svg>`;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'content';
    contentDiv.innerHTML = '<span class="typing-indicator">Thinking...</span>';

    messageDiv.appendChild(avatar);
    messageDiv.appendChild(contentDiv);
    wrapper.appendChild(messageDiv);

    chatContainer.appendChild(wrapper);

    const container = document.getElementById('chat-container');
    container.scrollTop = container.scrollHeight;

    return wrapper.id;
}

function removeMessage(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// Initialize
loadSessions();
