/* ThinkBuddy 手机端逻辑层
 * ---------------------------------------------------------------------------
 * 功能面严格按产品表裁剪：
 *   可做：chat(含 persona) / mastery_path·practice·question / immersive_reading /
 *         ask_questions / 技能 / 专家 / 拍照
 *   只读消费（只渲染、不发起）：visualize(SVG·Chart·Mermaid) /
 *         math_animator(Manim 视频) / video_lesson(Remotion+TTS 视频)
 *   不做：deep_research / deep_solve / co_writer / exec / 本地 CLI subagent /
 *         MCP 工具链 / 伙伴(partner) / SSH 远程
 *
 * 页面被原生层通过 shouldInterceptRequest 注入到 Next standalone 的同源
 * origin 下，因此 /api/* 与 /ws 直接走 web/proxy.ts 的重写，无 CORS 问题。
 */
(function () {
  'use strict';

  // ── 常量 ────────────────────────────────────────────────────────
  var CAPABILITIES = {
    chat: 'chat',
    ask: 'ask_questions',
    reading: 'immersive_reading'
  };
  // 只读消费：这些名字不出现在任何发起入口里，只在结果渲染中被识别。
  var READONLY_RENDERERS = ['visualize', 'math_animator', 'video_lesson'];

  var $ = function (id) { return document.getElementById(id); };
  var hasBridge = function () { return typeof window.ThinkBuddy !== 'undefined'; };
  var uid = function () {
    return 'm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  };

  // ── 状态 ────────────────────────────────────────────────────────
  var activeTab = 0;
  var activeCapability = CAPABILITIES.chat;
  var sessionId = null;
  var turnId = null;
  var lastSeq = 0;
  var ws = null;
  var streaming = false;
  var assistantNode = null;
  var pendingPhoto = null;
  var selectedSkills = [];
  var selectedPersona = null;
  var loaded = { practice: false, skills: false, experts: false, sessions: false };

  // ── 小工具 ──────────────────────────────────────────────────────
  function toast(message) {
    var node = $('toast');
    node.textContent = message;
    node.classList.remove('hidden');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { node.classList.add('hidden'); }, 2600);
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pick(obj) {
    for (var i = 1; i < arguments.length; i++) {
      var key = arguments[i];
      if (obj && obj[key] !== undefined && obj[key] !== null && obj[key] !== '') return obj[key];
    }
    return '';
  }

  function arr(value) { return Array.isArray(value) ? value : []; }

  function safeText(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'object') {
      return String(pick(value, 'content', 'text', 'title', 'name', 'label', 'question', 'prompt') || '');
    }
    return String(value);
  }

  async function apiGet(paths) {
    var list = Array.isArray(paths) ? paths : [paths];
    for (var i = 0; i < list.length; i++) {
      try {
        var res = await fetch(list[i], { headers: { Accept: 'application/json' } });
        if (res.ok) return await res.json();
      } catch (_) { /* 试下一个候选路径 */ }
    }
    return null;
  }

  async function apiPost(paths, body) {
    var list = Array.isArray(paths) ? paths : [paths];
    for (var i = 0; i < list.length; i++) {
      try {
        var res = await fetch(list[i], {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(body)
        });
        if (res.ok) return await res.json().catch(function () { return {}; });
      } catch (_) { /* 试下一个候选 */ }
    }
    return null;
  }

  function applyTheme() {
    if (!hasBridge() || !window.ThinkBuddy.theme) return;
    try {
      var t = JSON.parse(window.ThinkBuddy.theme());
      var root = document.documentElement.style;
      var map = {
        primary: 'primary', bg: 'bg', surface: 'surface',
        text: 'text', textSub: 'sub', divider: 'divider'
      };
      Object.keys(map).forEach(function (key) {
        if (t[key]) root.setProperty('--' + map[key], t[key]);
      });
      if (t.primary) root.setProperty('--user-bubble', t.primary);
    } catch (_) { /* 用 CSS 默认值 */ }
  }

  function setConn(text) { $('connState').textContent = text; }

  // ── WebSocket（/ws v2.0） ───────────────────────────────────────
  function wsSend(payload) {
    if (!ws || ws.readyState !== 1) { toast('连接未就绪，正在重连…'); connect(); return false; }
    ws.send(JSON.stringify(payload));
    return true;
  }

  function connect() {
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try { ws = new WebSocket(scheme + '//' + location.host + '/ws'); }
    catch (e) { setConn('无法建立连接'); return; }

    ws.onopen = function () {
      setConn('已连接');
      if (turnId && lastSeq > 0) {
        wsSend({ type: 'resume_from', protocol_version: '2.0', turn_id: turnId, seq: lastSeq });
      } else if (sessionId) {
        wsSend({ type: 'subscribe_session', protocol_version: '2.0', session_id: sessionId, after_seq: 0 });
      }
    };
    ws.onmessage = function (event) {
      try { handleFrame(JSON.parse(event.data)); } catch (_) { /* 忽略坏帧 */ }
    };
    ws.onerror = function () { setConn('连接异常'); };
    ws.onclose = function () {
      setConn('已断开，重连中…');
      ws = null;
      setTimeout(connect, 2000);
    };
  }

  function handleFrame(frame) {
    // 服务端可能直接发事件对象，也可能包一层 {event:...}/{data:...}。
    var ev = frame.event || frame.data || frame;
    var type = ev.type || frame.type;
    if (!type) return;
    var content = pick(ev, 'content', 'text', 'delta', 'message') || pick(frame, 'content', 'text') || '';
    var meta = ev.metadata || ev.meta || frame.metadata || {};
    var seq = Number(ev.seq !== undefined ? ev.seq : (frame.seq || 0));
    var incomingTurn = ev.turn_id || frame.turn_id || meta.turn_id;
    if (incomingTurn) turnId = incomingTurn;
    if (seq > 0) {
      if (seq <= lastSeq && type !== 'done') return;   // 幂等：不重复应用旧事件
      lastSeq = seq;
    }

    switch (type) {
      case 'session':
      case 'session_meta': {
        var sid = ev.session_id || frame.session_id || meta.session_id;
        if (sid) { sessionId = sid; }
        var title = pick(meta, 'title', 'session_title') || pick(ev, 'title');
        if (title) $('sessionTitle').textContent = safeText(title);
        if (!loaded.sessions) { loaded.sessions = true; loadSessions(); }
        break;
      }
      case 'content':
      case 'thinking':
      case 'observation':
        appendAssistant(safeText(content), type === 'thinking');
        break;
      case 'tool_call':
      case 'tool_result':
      case 'stage_start':
      case 'stage_end':
      case 'progress':
        appendToolCard(type, safeText(pick(meta, 'name', 'tool', 'stage', 'label')) || safeText(content));
        break;
      case 'sources':
        appendToolCard('引用', safeText(content) || '已检索到参考资料');
        break;
      case 'result':
        if (content) appendAssistant(safeText(content));
        if (assistantNode) renderRich(assistantNode.querySelector('.msg-inner'), safeText(content), meta);
        break;
      case 'wait_for_input':
        showAnswerCard(safeText(content) || safeText(pick(meta, 'question', 'prompt')), meta);
        break;
      case 'error':
        appendToolCard('错误', safeText(content) || safeText(pick(meta, 'message', 'error')));
        hintAccountError(ev.error_code);
        finishTurn();
        break;
      case 'protocol_error':
        appendToolCard('协议错误', safeText(pick(ev, 'message', 'error')) || safeText(content));
        hintAccountError(ev.error_code);
        finishTurn();
        break;
      case 'command_ack':
        if (ev.accepted === false || ev.ok === false) {
          toast('命令被服务端拒绝');
          finishTurn();
        }
        break;
      case 'pong':
      case 'active_turn_info':
        break;
      case 'done':
        finishTurn();
        break;
      default:
        break;
    }
  }

  // 没有可用模型时不要只丢一句英文错误：手机端的登录入口在原生设置里。
  function hintAccountError(code) {
    if (code !== 'no_llm_access' && code !== 'missing_provider_credentials') return;
    toast('还没有可用模型：请在设置里登录 Tokengine');
    if (hasBridge() && window.ThinkBuddy.openSettings) window.ThinkBuddy.openSettings();
  }

  function ensureAssistant() {
    if (assistantNode && assistantNode.isConnected) return assistantNode;
    var wrap = el('div', 'msg');
    var inner = el('div', 'msg-inner');
    inner.appendChild(el('div', 'msg-role', 'ThinkBuddy'));
    var body = el('div', 'msg-body');
    inner.appendChild(body);
    wrap.appendChild(inner);
    $('messages').appendChild(wrap);
    assistantNode = wrap;
    return wrap;
  }

  function appendAssistant(text, muted) {
    if (!text) return;
    var wrap = ensureAssistant();
    var body = wrap.querySelector('.msg-body');
    var line = el('div', muted ? 'tool-card' : '', text);
    body.appendChild(line);
    if (muted) line.style.opacity = '.7';
    scrollBottom();
  }

  function appendToolCard(label, text) {
    var wrap = ensureAssistant();
    var card = el('div', 'tool-card');
    card.appendChild(el('b', '', label + ' · '));
    card.appendChild(document.createTextNode(text || '处理中…'));
    wrap.querySelector('.msg-body').appendChild(card);
    scrollBottom();
  }

  function scrollBottom() {
    var box = $('messages');
    box.scrollTop = box.scrollHeight;
  }

  // ── 只读生成物：visualize / math_animator / video_lesson ──────────
  function renderRich(host, content, meta) {
    if (!host) return;
    var svgSource = pick(meta, 'svg', 'html', 'markup') ||
      (/<svg[\s>]/i.test(content) ? content : '');
    if (svgSource && /<svg[\s>]/i.test(svgSource)) {
      var svgHost = el('div', 'svg-host');
      // 内容来自本地引擎生成物；仍然剥掉脚本，避免生成物里的 HTML 被执行。
      svgHost.innerHTML = svgSource.replace(/<script[\s\S]*?<\/script>/gi, '');
      host.appendChild(svgHost);
    }

    var videoUrl = pick(meta, 'video_url', 'url', 'output_url', 'file_url', 'play_url');
    if (!videoUrl) {
      var match = String(content).match(/https?:\/\/\S+\.(?:mp4|webm|mov)(?:\?\S*)?/i);
      if (match) videoUrl = match[0];
    }
    if (videoUrl && /^(https?:\/\/|\/)/.test(videoUrl)) {
      var video = el('video');
      video.src = videoUrl;
      video.controls = true;
      video.playsInline = true;
      video.setAttribute('playsinline', '');
      host.appendChild(video);
    }

    // Mermaid / Chart.js：手机端只读消费，展示源码 + 说明，不引第三方渲染器。
    if (/```mermaid|```chart|"type"\s*:\s*"(?:chart|mermaid)"/i.test(content)) {
      var pre = el('pre', '', content);
      host.appendChild(pre);
      host.appendChild(el('div', 'tool-card', '交互版请在桌面端查看（手机只读消费）'));
    }
  }

  function showAnswerCard(question, meta) {
    var wrap = ensureAssistant();
    var card = el('div', 'answer-card');
    card.appendChild(el('div', 'q', question || '请作答'));
    var options = arr(meta.options || meta.choices || meta.questions);
    options.forEach(function (opt) {
      var label = safeText(opt);
      // 题目对象带 id 时必须回传 answers（questionId 是后端配对答案的唯一键），
      // 只回 text 会让多题追问对不上号。
      var questionId = (opt && typeof opt === 'object')
        ? String(opt.id || opt.questionId || opt.question_id || '')
        : '';
      var btn = el('button', '', label);
      btn.onclick = function () { submitReply(label, questionId); card.remove(); };
      card.appendChild(btn);
    });
    if (!options.length) {
      var input = el('input', 'quiz-input');
      input.placeholder = '输入你的回答';
      var submit = el('button', 'quiz-submit', '提交');
      submit.onclick = function () {
        var value = input.value.trim();
        if (!value) return;
        submitReply(value, String(meta.question_id || meta.questionId || ''));
        card.remove();
      };
      card.appendChild(input);
      card.appendChild(submit);
    }
    wrap.querySelector('.msg-body').appendChild(card);
    scrollBottom();
  }

  function submitReply(text, questionId) {
    if (!turnId) { toast('本轮尚未就绪'); return; }
    var payload = {
      type: 'submit_user_reply', protocol_version: '2.0',
      turn_id: turnId, command_id: uid()
    };
    if (questionId) payload.answers = [{ questionId: questionId, text: text }];
    else payload.text = text;
    wsSend(payload);
    appendMessage('user', text);
  }

  function finishTurn() {
    streaming = false;
    assistantNode = null;
    $('sendBtn').disabled = false;
    $('stopBtn').classList.add('hidden');
    $('regenBtn').classList.toggle('hidden', !sessionId);
    if (!loaded.sessions) { loaded.sessions = true; loadSessions(); }
  }

  // ── 发送消息 ────────────────────────────────────────────────────
  function appendMessage(role, text, imageUrl) {
    var wrap = el('div', 'msg' + (role === 'user' ? ' user' : ''));
    var inner = el('div', 'msg-inner');
    if (imageUrl) {
      var img = el('img');
      img.src = imageUrl;
      inner.appendChild(img);
    }
    if (text) inner.appendChild(el('div', '', text));
    wrap.appendChild(inner);
    $('messages').appendChild(wrap);
    scrollBottom();
  }

  async function toAttachment(url) {
    var res = await fetch(url);
    var blob = await res.blob();
    var base64 = await new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(',')[1] || ''); };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    return {
      type: 'image',
      base64: base64,
      filename: 'photo.jpg',
      mime_type: blob.type || 'image/jpeg'
    };
  }

  async function sendMessage() {
    var input = $('input');
    var text = input.value.trim();
    if (!text && !pendingPhoto) return;
    if (streaming) { toast('上一轮还在进行中'); return; }

    var imageUrl = pendingPhoto;
    appendMessage('user', text || '（图片）', imageUrl);
    input.value = '';
    input.style.height = 'auto';
    clearPendingPhoto();

    var payload = {
      type: 'start_turn',
      protocol_version: '2.0',
      content: text || '请分析这张图片',
      capability: activeCapability,
      session_id: sessionId || null,
      persona: selectedPersona || null,
      skills: selectedSkills.length ? selectedSkills.slice() : [],
      client_submission_id: uid(),
      config: {}
    };
    if (imageUrl) {
      try { payload.attachments = [await toAttachment(imageUrl)]; }
      catch (_) { toast('图片读取失败，已改为仅发送文字'); }
    }

    lastSeq = 0;
    turnId = null;
    streaming = true;
    ensureAssistant();
    $('sendBtn').disabled = true;
    $('stopBtn').classList.remove('hidden');
    $('regenBtn').classList.add('hidden');
    if (!wsSend(payload)) { streaming = false; finishTurn(); }
  }

  function stopTurn() {
    if (!turnId) return;
    wsSend({ type: 'cancel_turn', protocol_version: '2.0', turn_id: turnId, command_id: uid() });
  }

  function regenerate() {
    if (!sessionId) { toast('还没有会话'); return; }
    streaming = true;
    ensureAssistant();
    wsSend({ type: 'regenerate', protocol_version: '2.0', session_id: sessionId, overrides: {} });
  }

  // ── 会话列表 ────────────────────────────────────────────────────
  async function loadSessions() {
    var data = await apiGet(['/api/sessions?limit=30']);
    var list = $('sessionList');
    list.innerHTML = '';
    var rows = arr(data && (data.sessions || data.items || data));
    if (!rows.length) { list.appendChild(el('div', 'empty', '还没有会话')); return; }
    rows.slice(0, 30).forEach(function (row) {
      var id = String(pick(row, 'id', 'session_id', 'sourceId') || '');
      if (!id) return;
      var item = el('div', 'drawer-item');
      item.appendChild(document.createTextNode(
        safeText(pick(row, 'title', 'name', 'subject')) || '未命名会话'
      ));
      item.appendChild(el('small', '', safeText(pick(row, 'updated_at', 'created_at', 'last_active'))));
      item.onclick = function () { openSession(id); $('sessionDrawer').classList.add('hidden'); };
      list.appendChild(item);
    });
  }

  async function openSession(id) {
    var data = await apiGet(['/api/sessions/' + encodeURIComponent(id)]);
    if (!data) { toast('加载会话失败'); return; }
    sessionId = id;
    $('sessionTitle').textContent = safeText(pick(data, 'title', 'name')) || '对话';
    $('messages').innerHTML = '';
    var messages = arr(data.messages || (data.session && data.session.messages) || data.items);
    messages.forEach(function (message) {
      var role = String(pick(message, 'role', 'sender', 'type') || '');
      var body = safeText(pick(message, 'content', 'text', 'body', 'message'));
      if (body) appendMessage(role === 'user' || role === 'human' ? 'user' : 'assistant', body);
    });
    if (!messages.length) $('messages').appendChild(el('div', 'empty', '这条会话暂无消息'));
    lastSeq = 0;
    turnId = null;
  }

  function newChat() {
    sessionId = null;
    turnId = null;
    lastSeq = 0;
    $('sessionTitle').textContent = '新对话';
    $('messages').innerHTML = '';
    $('messages').appendChild(el('div', 'empty', '开始一个新对话吧'));
    $('regenBtn').classList.add('hidden');
  }

  // ── 练习：practice / question / mastery_path ────────────────────
  async function loadPractice() {
    loaded.practice = true;
    var summary = await apiGet(['/api/practice/summary']);
    var stats = $('practiceSummary');
    stats.innerHTML = '';
    // 字段与 PracticeSummary 一一对应：due / reviewed_today / mistakes。
    // 不做「连续天数」「掌握主题」这类后端并不返回的数字，避免首页长期显示 —。
    var values = [
      [pick(summary || {}, 'due'), '今日待复习'],
      [pick(summary || {}, 'reviewed_today'), '今日已复习'],
      [pick(summary || {}, 'mistakes'), '累计错题']
    ];
    values.forEach(function (pair) {
      var raw = pair[0];
      var shown = (raw === '' || raw === null || raw === undefined) ? '—' : raw;
      var box = el('div', 'stat');
      box.appendChild(el('b', '', shown));
      box.appendChild(el('span', '', pair[1]));
      stats.appendChild(box);
    });

    var queue = await apiGet(['/api/practice/queue?limit=12']);
    var queueBox = $('practiceQueue');
    queueBox.innerHTML = '';
    var rows = arr(queue && (queue.questions || queue.items || queue));
    if (!rows.length) {
      queueBox.appendChild(el('div', 'empty', '题库里还没有待复习的题目'));
      var cta = el('button', 'quiz-submit', '让 AI 出 5 道题');
      cta.onclick = function () { startQuestionTurn(); };
      queueBox.appendChild(cta);
    } else {
      rows.slice(0, 12).forEach(function (row, index) { queueBox.appendChild(quizCard(row, index)); });
    }

    var topicsData = await apiGet([
      '/api/mastery_path/topics', '/api/mastery-path/topics', '/api/mastery/topics'
    ]);
    var topicsBox = $('masteryTopics');
    topicsBox.innerHTML = '';
    var topics = arr(topicsData && (topicsData.topics || topicsData.items || topicsData));
    if (!topics.length) {
      topicsBox.appendChild(el('div', 'empty', '暂无掌握路径，可在对话里创建主题'));
    } else {
      topics.slice(0, 12).forEach(function (topic) {
        var card = el('div', 'card');
        card.appendChild(el('div', 'card-title',
          safeText(pick(topic, 'title', 'name', 'topic')) || '未命名主题'));
        var progress = pick(topic, 'progress', 'mastery', 'completion');
        card.appendChild(el('div', 'card-desc',
          progress !== '' ? ('掌握度 ' + progress) : '点击进入对话继续学习'));
        card.onclick = function () {
          activeCapability = CAPABILITIES.chat;
          syncChips();
          $('input').value = '继续学习主题：' + safeText(pick(topic, 'title', 'name', 'topic'));
          goTab(0);
        };
        topicsBox.appendChild(card);
      });
    }
  }

  function quizCard(row, index) {
    var card = el('div', 'card');
    var entry = row.entry || row.question || row;
    // entry_id 是 int 型路径参数，取不到就不能提交（否则请求变成 /questions//check）。
    var entryId = pick(row, 'entry_id', 'id') || pick(entry, 'id', 'entry_id') || '';
    var question = safeText(pick(entry, 'question', 'prompt', 'content', 'front', 'title')) ||
      ('第 ' + (index + 1) + ' 题');
    card.appendChild(el('div', 'card-title', question));
    var hint = safeText(pick(entry, 'hint', 'subject', 'category', 'tags'));
    if (hint) card.appendChild(el('div', 'card-desc', hint));

    var input = el('input', 'quiz-input');
    input.placeholder = '输入答案后提交';
    var submit = el('button', 'quiz-submit', '提交判分');
    var result = el('div', 'quiz-result');
    if (!entryId) {
      input.disabled = true;
      submit.disabled = true;
      result.textContent = '这道题缺少题库 id，只能去「对话」里练';
    }
    submit.onclick = async function () {
      var value = input.value.trim();
      if (!value) { toast('先写个答案'); return; }
      result.textContent = '判分中…';
      // POST /api/practice/questions/{entry_id}/check 只接受 {answer}。
      var data = await apiPost(
        '/api/practice/questions/' + encodeURIComponent(entryId) + '/check',
        { answer: value }
      );
      if (!data) { result.textContent = '判分接口不可用，可在「对话」里粘贴题目求解'; return; }
      var correct = data.correct;
      result.textContent = correct === true ? '✅ 正确' : correct === false ? '❌ 再想想' : '已提交';
    };
    card.appendChild(input);
    card.appendChild(submit);
    card.appendChild(result);
    return card;
  }

  /**
   * 出题入口。`deep_question` 是后端注册的能力名（表里写的 "question" 是它的内部
   * 产物）：走一轮普通 start_turn，生成的题会进题库，练习页再刷。
   */
  function startQuestionTurn() {
    activeCapability = 'deep_question';
    syncChips();
    $('input').value = '根据我最近的学习内容出 5 道题，难度中等，每题给出答案与考点。';
    goTab(0);
    toast('已切到出题模式，点发送即可');
  }

  // ── 技能 / 专家 ─────────────────────────────────────────────────
  async function loadSkills() {
    loaded.skills = true;
    var data = await apiGet(['/api/skills/list', '/api/skills']);
    var box = $('skillList');
    box.innerHTML = '';
    var rows = arr(data && (data.skills || data.items || data));
    if (!rows.length) { box.appendChild(el('div', 'empty', '暂无技能')); return; }
    rows.forEach(function (row) {
      var name = String(pick(row, 'name', 'id', 'slug') || '');
      if (!name) return;
      var card = el('div', 'card');
      card.dataset.name = name;
      card.appendChild(el('div', 'card-title',
        safeText(pick(row, 'display_name', 'title', 'label')) || name));
      card.appendChild(el('div', 'card-desc',
        safeText(pick(row, 'description', 'summary', 'desc')) || '点击选用'));
      card.classList.toggle('selected', selectedSkills.indexOf(name) >= 0);
      card.onclick = function () {
        var at = selectedSkills.indexOf(name);
        if (at >= 0) selectedSkills.splice(at, 1); else selectedSkills.push(name);
        card.classList.toggle('selected', at < 0);
        updatePickerNote();
      };
      box.appendChild(card);
    });
  }

  async function loadExperts() {
    loaded.experts = true;
    var data = await apiGet(['/api/personas/personas', '/api/personas']);
    var box = $('expertList');
    box.innerHTML = '';
    var rows = arr(data && (data.personas || data.items || data));
    if (!rows.length) { box.appendChild(el('div', 'empty', '暂无专家')); return; }
    rows.forEach(function (row) {
      var name = String(pick(row, 'name', 'id', 'slug') || '');
      if (!name) return;
      var card = el('div', 'card');
      card.appendChild(el('div', 'card-title',
        safeText(pick(row, 'display_name', 'title', 'label')) || name));
      card.appendChild(el('div', 'card-desc',
        safeText(pick(row, 'description', 'summary', 'prompt', 'desc')) || '点击设为当前专家'));
      card.classList.toggle('selected', selectedPersona === name);
      card.onclick = function () {
        selectedPersona = selectedPersona === name ? null : name;
        box.querySelectorAll('.card').forEach(function (node) {
          node.classList.toggle('selected', node.dataset.name === selectedPersona);
        });
        updatePickerNote();
      };
      card.dataset.name = name;
      box.appendChild(card);
    });
  }

  function updatePickerNote() {
    var bits = [];
    if (selectedPersona) bits.push('专家：' + selectedPersona);
    if (selectedSkills.length) bits.push('技能：' + selectedSkills.join('、'));
    $('pickerNote').textContent = bits.length ? bits.join('　·　') + '（本轮生效）' : '';
  }

  function syncChips() {
    document.querySelectorAll('#capChips .chip').forEach(function (chip) {
      chip.classList.toggle('active', chip.dataset.cap === activeCapability);
    });
  }

  // ── 拍照 ────────────────────────────────────────────────────────
  function onPhoto(url) {
    pendingPhoto = null;
    var preview = $('photoPreview');
    preview.src = url;
    preview.classList.remove('hidden');
    $('cameraPlaceholder').classList.add('hidden');
    $('sendPhotoBtn').classList.remove('hidden');
    $('sendPhotoBtn').dataset.url = url;
  }

  function clearPendingPhoto() {
    pendingPhoto = null;
    $('attachPreview').classList.add('hidden');
  }

  function usePhotoInChat() {
    var url = $('sendPhotoBtn').dataset.url;
    if (!url) return;
    pendingPhoto = url;
    $('attachThumb').src = url;
    $('attachPreview').classList.remove('hidden');
    goTab(0);
    toast('图片已加入输入区，补充文字后发送');
  }

  function fallbackPicker() {
    var node = $('fallbackFile');
    if (node) node.click();
    else toast('当前环境不支持选择图片');
  }

  function takePhoto() {
    if (hasBridge() && window.ThinkBuddy.takePhoto) window.ThinkBuddy.takePhoto();
    else fallbackPicker();
  }

  function pickImage() {
    if (hasBridge() && window.ThinkBuddy.pickImage) window.ThinkBuddy.pickImage();
    else fallbackPicker();
  }

  // ── Tab 切换（原生导航会调用 window.__tb.setTab） ────────────────
  function setTab(index) {
    activeTab = index;
    var views = document.querySelectorAll('.view');
    for (var i = 0; i < views.length; i++) views[i].classList.toggle('active', i === index);
    if (index === 1 && !loaded.practice) loadPractice();
    if (index === 2 && !loaded.skills) loadSkills();
    if (index === 3 && !loaded.experts) loadExperts();
    if (index === 0 && !loaded.sessions) { loaded.sessions = true; loadSessions(); }
  }

  function goTab(index) {
    setTab(index);
    if (hasBridge() && window.ThinkBuddy.goToTab) window.ThinkBuddy.goToTab(index);
  }

  // ── 绑定 ────────────────────────────────────────────────────────
  function bind() {
    $('sendBtn').onclick = sendMessage;
    $('stopBtn').onclick = stopTurn;
    $('regenBtn').onclick = regenerate;
    $('newChatBtn').onclick = newChat;
    $('sessionsBtn').onclick = function () {
      $('sessionDrawer').classList.remove('hidden');
      loadSessions();
    };
    $('closeDrawer').onclick = function () { $('sessionDrawer').classList.add('hidden'); };
    $('input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage(); }
    });
    $('input').addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 110) + 'px';
    });
    $('attachBtn').onclick = function () { goTab(4); };
    $('attachRemove').onclick = clearPendingPhoto;

    document.querySelectorAll('#capChips .chip').forEach(function (chip) {
      chip.onclick = function () {
        activeCapability = chip.dataset.cap;
        syncChips();
        toast('已切换：' + chip.textContent.trim());
      };
    });

    $('practiceRefresh').onclick = function () { loaded.practice = false; loadPractice(); };
    $('skillsRefresh').onclick = function () { loaded.skills = false; loadSkills(); };
    $('expertsRefresh').onclick = function () { loaded.experts = false; loadExperts(); };
    $('takePhotoBtn').onclick = takePhoto;
    $('pickImageBtn').onclick = pickImage;
    $('sendPhotoBtn').onclick = usePhotoInChat;

    // 原生桥不可用时的降级：用 <input type=file> 的本地 object URL。
    var fallback = el('input');
    fallback.type = 'file';
    fallback.accept = 'image/*';
    fallback.id = 'fallbackFile';
    fallback.style.display = 'none';
    fallback.onchange = function () {
      var file = fallback.files && fallback.files[0];
      if (file) onPhoto(URL.createObjectURL(file));
    };
    document.body.appendChild(fallback);
  }

  // 暴露给原生 MainActivity 的 JS Bridge。
  window.__tb = {
    setTab: setTab,
    onPhoto: onPhoto,
    toast: toast,
    refresh: function () {
      loaded.practice = loaded.skills = loaded.experts = loaded.sessions = false;
      setTab(activeTab);
    }
  };

  applyTheme();
  bind();
  setTab(0);
  connect();
})();
