/* 零构建智能体调试客户端：对话长连接、工具短连接和可分叉调用树。 */
(function () {
    'use strict';

    const ENDPOINTS = {
        getWidgetCapabilityOverview: '/api/v1/ws/tools/getWidgetCapabilityOverview',
        getDataCapabilitySchemas: '/api/v1/ws/tools/getDataCapabilitySchemas',
        generateWidgetCardCompactDsl: '/api/v1/ws/tools/generateWidgetCardCompactDsl'
    };
    const SESSION_STORAGE_KEY = 'ai-widget-agent-conversation-id';
    const NODE_WIDTH = 174;
    const NODE_HEIGHT = 70;

    const state = {
        socket: null,
        conversationId: sessionStorage.getItem(SESSION_STORAGE_KEY),
        activeTurnId: null,
        lastUserText: '',
        pendingText: null,
        toolSockets: new Map(),
        turnContext: null,
        nodes: new Map(),
        selectedCallId: null,
        manualIds: new Set(),
        activeTranscript: [],
        activeCallId: null
    };

    const byId = (id) => document.getElementById(id);
    const serverUrl = () => byId('serverUrl').value.trim().replace(/\/$/, '');

    function appendMessageNode(kind, content) {
        const node = document.createElement('div');
        node.className = `agent-message ${kind}`;
        node.textContent = content;
        const list = byId('agentMessages');
        list.appendChild(node);
        list.scrollTop = list.scrollHeight;
    }

    function addMessage(kind, content) {
        state.activeTranscript.push({ kind, content });
        appendMessageNode(kind, content);
        const activeNode = state.nodes.get(state.activeCallId);
        if (activeNode) activeNode.chatSnapshot = cloneTranscript(state.activeTranscript);
    }

    function cloneTranscript(transcript) {
        return transcript.map((message) => ({ kind: message.kind, content: message.content }));
    }

    function renderTranscript() {
        byId('agentMessages').innerHTML = '';
        state.activeTranscript.forEach((message) => appendMessageNode(message.kind, message.content));
    }

    function setStatus(value) {
        byId('agentSessionStatus').textContent = value;
        const active = Boolean(state.activeTurnId);
        byId('agentSendBtn').disabled = active;
        byId('agentStopBtn').disabled = !active;
        byId('agentReplayBtn').disabled = active || !selectedReplayableNode();
    }

    function captureTurnContext() {
        return {
            deviceInfo: {
                countryCode: byId('countryCode').value,
                deviceFormation: byId('deviceFormation').value,
                deviceType: byId('deviceType').value,
                locale: byId('locale').value,
                phoneType: byId('phoneType').value,
                prdVer: byId('prdVer').value,
                sysVer: byId('sysVer').value,
                romVersion: byId('romVersion').value,
                time: byId('time').value
            },
            userId: byId('userId').value,
            bundleName: byId('bundleName').value,
            version: byId('version').value
        };
    }

    function buildEnvelope(argumentsData, callId) {
        const context = state.turnContext || captureTurnContext();
        const content = Object.assign({}, argumentsData);
        if (!content.bundleName) content.bundleName = context.bundleName;
        return {
            content,
            deviceInfo: context.deviceInfo,
            session: { sessionId: state.conversationId, interactionId: callId, isNew: false },
            userAuth: { user: { userId: context.userId } },
            utterance: { original: state.lastUserText, type: 'text' },
            version: context.version,
            bundleName: context.bundleName
        };
    }

    function parseFullLegacy(content) {
        let data = null;
        try {
            if (typeof PythonReprParser !== 'undefined') data = PythonReprParser.parse(content);
        } catch (error) {
            data = null;
        }
        const quoted = (name) => {
            const match = content.match(new RegExp(`(?:^|[,\\s(])${name}='([^']*)'`));
            return match ? match[1] : '';
        };
        return {
            type: quoted('type'),
            status: quoted('status'),
            errorCode: quoted('errorCode'),
            error: quoted('error') || null,
            data
        };
    }

    function executeTool(event) {
        const path = ENDPOINTS[event.functionName];
        if (!path) {
            return Promise.resolve({
                invokeStatus: 'failed',
                error: { code: 'CLIENT_TOOL_NOT_ALLOWED', message: '工具不在前端白名单内。' }
            });
        }
        const envelope = buildEnvelope(event.arguments || {}, event.callId);
        const startedAt = performance.now();
        return new Promise((resolve) => {
            const socket = new WebSocket(`${serverUrl()}${path}`);
            let finished = false;
            const finish = (result) => {
                if (finished) return;
                finished = true;
                state.toolSockets.delete(event.callId);
                result.durationMs = Math.round(performance.now() - startedAt);
                resolve(result);
            };
            state.toolSockets.set(event.callId, socket);
            socket.onopen = () => socket.send(JSON.stringify(envelope));
            socket.onmessage = (message) => {
                let frame;
                try {
                    frame = JSON.parse(message.data);
                } catch (error) {
                    finish({ invokeStatus: 'failed', error: { code: 'CLIENT_FRAME_INVALID', message: String(error) } });
                    return;
                }
                const streamInfo = frame.reply && frame.reply.streamInfo;
                if (!streamInfo || streamInfo.streamType !== 'final') return;
                finish({ invokeStatus: 'succeeded', response: parseFullLegacy(streamInfo.streamContent || '') });
                socket.close();
            };
            socket.onerror = () => finish({
                invokeStatus: 'failed',
                error: { code: 'CLIENT_WEBSOCKET_ERROR', message: '工具 WebSocket 连接失败。' }
            });
            socket.onclose = () => {
                if (!finished) finish({
                    invokeStatus: 'failed',
                    error: { code: 'CLIENT_WEBSOCKET_CLOSED', message: '工具连接在最终结果前关闭。' }
                });
            };
            window.setTimeout(() => {
                if (!finished) {
                    socket.close();
                    finish({
                        invokeStatus: 'failed',
                        error: { code: 'CLIENT_TOOL_TIMEOUT', message: '工具调用超时。' }
                    });
                }
            }, 180000);
        });
    }

    function upsertToolNode(event) {
        const current = state.nodes.get(event.callId) || {};
        const isNewNode = !current.callId;
        if (isNewNode && event.replayedFromCallId) {
            const sourceNode = state.nodes.get(event.replayedFromCallId);
            if (sourceNode) {
                state.activeTranscript = cloneTranscript(sourceNode.chatBefore || []);
                renderTranscript();
            }
        }
        const result = event.result || current.result || null;
        const businessStatus = result && result.response && result.response.data
            ? result.response.data.status
            : null;
        let nodeStatus = 'pending';
        if (result) {
            nodeStatus = result.invokeStatus === 'failed' ? 'failed' : 'succeeded';
            if (businessStatus === 'failed' || businessStatus === 'unsupported') {
                nodeStatus = 'failed';
            }
        }
        state.nodes.set(event.callId, {
            callId: event.callId,
            parentCallId: event.parentCallId === undefined ? current.parentCallId : event.parentCallId,
            replayedFromCallId: event.replayedFromCallId || current.replayedFromCallId || null,
            functionName: event.functionName || current.functionName,
            arguments: event.arguments || current.arguments || {},
            result,
            durationMs: result && result.durationMs,
            status: nodeStatus,
            origin: 'agent',
            order: current.order || state.nodes.size + 1,
            chatBefore: current.chatBefore || cloneTranscript(state.activeTranscript),
            chatSnapshot: current.chatSnapshot || cloneTranscript(state.activeTranscript)
        });
        if (isNewNode) state.activeCallId = event.callId;
        renderFlow();
        if (state.selectedCallId === event.callId) renderSelectedNode();
    }

    function syncManualHistory() {
        if (typeof HistoryManager === 'undefined') return;
        let previousId = null;
        let changed = false;
        HistoryManager.getAll().forEach((entry) => {
            const callId = `manual-${entry.id}`;
            if (!state.manualIds.has(callId)) {
                state.manualIds.add(callId);
                state.nodes.set(callId, {
                    callId,
                    parentCallId: previousId,
                    replayedFromCallId: null,
                    functionName: entry.interfaceName,
                    arguments: entry.request,
                    result: entry.parsedResult,
                    status: entry.parsedResult ? 'succeeded' : 'failed',
                    origin: 'manual',
                    order: state.nodes.size + 1
                });
                changed = true;
            }
            previousId = callId;
        });
        if (changed) renderFlow();
    }

    function renderFlow() {
        const canvas = byId('agentFlowCanvas');
        if (!state.nodes.size) {
            canvas.innerHTML = '<div class="flow-empty">暂无接口调用</div>';
            return;
        }
        const nodes = Array.from(state.nodes.values()).sort((a, b) => a.order - b.order);
        const depths = new Map();
        const depthOf = (node) => {
            if (depths.has(node.callId)) return depths.get(node.callId);
            const parent = state.nodes.get(node.parentCallId);
            const depth = parent ? depthOf(parent) + 1 : 0;
            depths.set(node.callId, depth);
            return depth;
        };
        nodes.forEach(depthOf);
        const rowsByDepth = new Map();
        const positions = new Map();
        nodes.forEach((node) => {
            const depth = depths.get(node.callId);
            const row = rowsByDepth.get(depth) || 0;
            rowsByDepth.set(depth, row + 1);
            positions.set(node.callId, { x: 18 + depth * NODE_WIDTH, y: 18 + row * NODE_HEIGHT });
        });
        const width = Math.max(...Array.from(depths.values())) * NODE_WIDTH + NODE_WIDTH + 18;
        const height = Math.max(...Array.from(rowsByDepth.values())) * NODE_HEIGHT + 18;
        canvas.innerHTML = '';
        canvas.style.minWidth = `${width}px`;
        canvas.style.height = `${Math.max(112, height)}px`;
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', width);
        svg.setAttribute('height', Math.max(112, height));
        nodes.forEach((node) => {
            const parentPosition = positions.get(node.parentCallId);
            if (!parentPosition) return;
            const position = positions.get(node.callId);
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            const startX = parentPosition.x + 154;
            const startY = parentPosition.y + 29;
            const endX = position.x;
            const endY = position.y + 29;
            const midX = (startX + endX) / 2;
            path.setAttribute('d', `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`);
            path.setAttribute('class', `flow-edge${node.replayedFromCallId ? ' replay' : ''}`);
            svg.appendChild(path);
        });
        canvas.appendChild(svg);
        nodes.forEach((node) => {
            const position = positions.get(node.callId);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `flow-node ${node.status}${node.replayedFromCallId ? ' replay' : ''}${state.selectedCallId === node.callId ? ' selected' : ''}`;
            button.style.left = `${position.x}px`;
            button.style.top = `${position.y}px`;
            const shortName = node.functionName.replace('getWidgetCapabilityOverview', 'Overview')
                .replace('getDataCapabilitySchemas', 'Schema')
                .replace('generateWidgetCardCompactDsl', 'Generate');
            button.innerHTML = `<strong>${shortName}</strong><small>${node.origin === 'manual' ? '手工' : node.status}${node.durationMs ? ` · ${node.durationMs} ms` : ''}</small>`;
            button.addEventListener('click', () => selectNode(node.callId));
            canvas.appendChild(button);
        });
    }

    function selectNode(callId) {
        const node = state.nodes.get(callId);
        if (node && node.origin === 'agent' && state.activeTurnId) {
            addMessage('system', '当前轮次仍在执行，请先停止或等待完成后再切换节点上下文。');
            return;
        }
        state.selectedCallId = callId;
        if (node && node.origin === 'manual' && typeof HistoryManager !== 'undefined') {
            HistoryManager.load(Number(callId.replace('manual-', '')));
        } else if (node && node.origin === 'agent') {
            state.activeTranscript = cloneTranscript(node.chatSnapshot || []);
            state.activeCallId = callId;
            renderTranscript();
            if (!state.activeTurnId && node.status !== 'pending') {
                send({ type: 'conversation.checkout', callId });
                setStatus('正在切换节点上下文…');
            }
        }
        renderFlow();
        renderSelectedNode();
    }

    function selectedReplayableNode() {
        const node = state.nodes.get(state.selectedCallId);
        return node && node.origin === 'agent' && node.status !== 'pending';
    }

    function renderSelectedNode() {
        const node = state.nodes.get(state.selectedCallId);
        byId('agentNodeEmpty').classList.toggle('hidden', Boolean(node));
        byId('agentNodeDetail').classList.toggle('hidden-mode', !node);
        if (!node) return;
        byId('agentNodeStatus').textContent = `${node.functionName} · ${node.status}`;
        byId('agentNodeRequest').value = JSON.stringify(node.arguments, null, 2);
        byId('agentNodeResponse').textContent = node.result
            ? JSON.stringify(node.result.response || node.result.error || node.result, null, 2)
            : '等待接口返回…';
        byId('agentReplayBtn').disabled = Boolean(state.activeTurnId) || !selectedReplayableNode();
        byId('agentReplayBtn').title = node.origin === 'manual' ? '手工调用不属于智能体会话，不能分支重放。' : '';
    }

    function replaySelectedNode() {
        const node = state.nodes.get(state.selectedCallId);
        if (!node || !selectedReplayableNode()) return;
        let argumentsData;
        try {
            argumentsData = JSON.parse(byId('agentNodeRequest').value);
        } catch (error) {
            addMessage('system', `调用参数不是合法 JSON：${error.message}`);
            return;
        }
        if (!argumentsData || Array.isArray(argumentsData) || typeof argumentsData !== 'object') {
            addMessage('system', '调用参数必须是 JSON 对象。');
            return;
        }
        state.turnContext = captureTurnContext();
        state.lastUserText = `从 ${node.functionName} 节点重放`;
        const sent = send({
            type: 'turn.replay',
            sourceCallId: node.callId,
            functionName: node.functionName,
            arguments: argumentsData
        });
        if (sent) {
            state.activeTurnId = 'replay-pending';
            setStatus('正在创建重放分支…');
        } else {
            addMessage('system', '智能体连接未建立，无法重放节点。');
        }
    }

    function send(payload) {
        if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return false;
        state.socket.send(JSON.stringify(payload));
        return true;
    }

    function openConversation() {
        if (state.socket && state.socket.readyState === WebSocket.OPEN) {
            send({ type: 'conversation.open', conversationId: state.conversationId || undefined });
            return;
        }
        setStatus('正在连接智能体…');
        const socket = new WebSocket(`${serverUrl()}/api/v1/ws/agent/chat`);
        state.socket = socket;
        socket.onopen = () => send({ type: 'conversation.open', conversationId: state.conversationId || undefined });
        socket.onmessage = (message) => {
            try {
                handleEvent(JSON.parse(message.data));
            } catch (error) {
                addMessage('system', `智能体事件解析失败：${error}`);
            }
        };
        socket.onerror = () => setStatus('智能体连接失败');
        socket.onclose = () => {
            if (!state.activeTurnId) setStatus('智能体已断开');
        };
    }

    function handleEvent(event) {
        if (event.type === 'conversation.ready') {
            state.conversationId = event.conversationId;
            sessionStorage.setItem(SESSION_STORAGE_KEY, state.conversationId);
            setStatus(event.resumed ? '已恢复会话' : '已创建会话');
            if (state.pendingText) {
                const pendingText = state.pendingText;
                state.pendingText = null;
                send({ type: 'turn.start', text: pendingText });
            }
            return;
        }
        if (event.type === 'assistant.message') {
            addMessage('assistant', event.content);
            return;
        }
        if (event.type === 'tool.call') {
            upsertToolNode(event);
            executeTool(event).then((result) => send({
                type: 'tool.result',
                conversationId: event.conversationId,
                turnId: event.turnId,
                callId: event.callId,
                result
            }));
            return;
        }
        if (event.type === 'tool.trace') {
            if (ENDPOINTS[event.functionName]) upsertToolNode(event);
            return;
        }
        if (event.type === 'turn.status') {
            if (event.turnId && event.status === 'accepted') state.activeTurnId = event.turnId;
            setStatus(`本轮状态：${event.status}`);
            return;
        }
        if (event.type === 'turn.completed') {
            state.activeTurnId = null;
            setStatus(`本轮已${event.status === 'completed' ? '完成' : event.status}`);
            return;
        }
        if (event.type === 'conversation.checked_out') {
            setStatus('已切换到所选节点上下文');
            return;
        }
        if (event.type === 'error') {
            if (event.code === 'TURN_REPLAY_REJECTED') {
                state.activeTurnId = null;
                setStatus('节点重放被拒绝');
            }
            addMessage('system', `${event.code || 'ERROR'}：${event.message}`);
        }
    }

    function sendTurn() {
        const input = byId('agentInput');
        const text = input.value.trim();
        if (!text || state.activeTurnId) return;
        state.lastUserText = text;
        state.turnContext = captureTurnContext();
        state.activeCallId = null;
        addMessage('user', text);
        input.value = '';
        if (!send({ type: 'turn.start', text })) {
            state.pendingText = text;
            openConversation();
        }
    }

    function stopTurn() {
        if (state.activeTurnId) send({ type: 'turn.cancel', turnId: state.activeTurnId });
        state.toolSockets.forEach((socket) => socket.close());
        state.toolSockets.clear();
    }

    function resetConversation() {
        stopTurn();
        if (send({ type: 'conversation.reset' })) {
            state.activeTranscript = [];
            state.activeCallId = null;
            renderTranscript();
            state.nodes.forEach((node, callId) => {
                if (node.origin === 'agent') state.nodes.delete(callId);
            });
            state.selectedCallId = null;
            renderFlow();
            renderSelectedNode();
        }
    }

    function setMode(mode) {
        const agentMode = mode === 'agent';
        byId('agentDebugger').classList.toggle('hidden-mode', !agentMode);
        document.querySelectorAll('.manual-debugger').forEach((node) => node.classList.toggle('hidden-mode', agentMode));
        byId('agentModeButton').classList.toggle('active', agentMode);
        byId('manualModeButton').classList.toggle('active', !agentMode);
        if (agentMode) openConversation();
    }

    function init() {
        byId('agentModeButton').addEventListener('click', () => setMode('agent'));
        byId('manualModeButton').addEventListener('click', () => setMode('manual'));
        byId('agentSendBtn').addEventListener('click', sendTurn);
        byId('agentStopBtn').addEventListener('click', stopTurn);
        byId('agentResetBtn').addEventListener('click', resetConversation);
        byId('agentReplayBtn').addEventListener('click', replaySelectedNode);
        byId('agentInput').addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) sendTurn();
        });
        window.setInterval(syncManualHistory, 750);
        setStatus('未连接');
        renderFlow();
        renderSelectedNode();
    }

    document.addEventListener('DOMContentLoaded', init);
})();
