"""rating.py — Scene rating page for PeaceGrappler."""

from flask import Blueprint, jsonify, request

from db import get_all_scenes, get_scene_grades, save_grade

rating_bp = Blueprint("rating", __name__)


@rating_bp.route("/rate")
def rate_page():
    return RATE_HTML


@rating_bp.route("/rate/api/scenes")
def api_scenes():
    """Return all scenes with grade info for rating."""
    scenes = get_all_scenes(include_ignored=True)
    grades = get_scene_grades()
    tag = request.args.get("tag", "")

    result = []
    for s in scenes:
        if tag and tag not in s["tags"]:
            continue
        dur = round(s["end_time"] - s["start_time"], 1)
        grade_info = grades.get(s["id"])
        avg = round(grade_info["total_score"] / grade_info["times_graded"], 1) \
            if grade_info else None
        result.append({
            "id": s["id"],
            "filename": s["video_filename"],
            "start": s["start_time"],
            "end": s["end_time"],
            "duration": dur,
            "tags": s["tags"],
            "wide": s["wide"],
            "avg_grade": avg,
            "times_graded": grade_info["times_graded"] if grade_info else 0,
        })
    return jsonify(result)


@rating_bp.route("/rate/api/grade", methods=["POST"])
def api_grade():
    data = request.json or {}
    scene_id = data.get("scene_id")
    score = data.get("score")
    if not scene_id or score not in (1, 2, 3, 4, 5):
        return jsonify({"error": "scene_id and score (1-5) required"}), 400
    save_grade(scene_id, score)
    return jsonify({"status": "ok"})


@rating_bp.route("/rate/api/tags")
def api_tags():
    scenes = get_all_scenes(include_ignored=True)
    tag_counts = {}
    for s in scenes:
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    return jsonify(dict(sorted(tag_counts.items())))


RATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PeaceGrappler - Rate Scenes</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0a0a;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:flex;flex-direction:column;min-height:100vh;
}
header{
  background:#141414;border-bottom:1px solid #2a2a2a;
  padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0;
}
header h1{font-size:18px;font-weight:600;color:#fff;white-space:nowrap}
header h1 span{color:#e53935}
nav{display:flex;gap:8px;margin-left:auto;flex-shrink:0}
nav a{color:#aaa;text-decoration:none;font-size:12px;padding:4px 8px;
  border:1px solid #444;border-radius:6px}
nav a:hover{color:#fff;border-color:#888}
nav a.active{color:#e53935;border-color:#e53935}

.content{flex:1;display:flex;flex-direction:column;align-items:center;padding:24px}

/* -- Controls -- */
.controls{
  display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap;
}
.controls label{font-size:12px;color:#888;font-weight:600;text-transform:uppercase}
select{
  background:#222;color:#e0e0e0;border:1px solid #444;border-radius:6px;
  padding:6px 12px;font-size:13px;
}
select:focus{outline:none;border-color:#e53935}
.progress-text{font-size:13px;color:#888}

/* -- Rating card -- */
.rate-card{
  background:#141414;border:1px solid #2a2a2a;border-radius:12px;
  width:100%;max-width:400px;overflow:hidden;
}
.rate-card .thumb-wrap{
  position:relative;width:100%;aspect-ratio:9/16;background:#111;
}
.rate-card .thumb-wrap img{
  width:100%;height:100%;object-fit:cover;display:block;
}
.rate-card .meta{padding:14px 16px}
.rate-card .meta .filename{font-size:12px;color:#666;margin-bottom:4px}
.rate-card .meta .tags{font-size:13px;color:#aaa;margin-bottom:4px}
.rate-card .meta .dur{font-size:12px;color:#555}
.rate-card .meta .prev-grade{
  font-size:12px;color:#818cf8;margin-top:4px;
}

/* -- Star buttons -- */
.stars{
  display:flex;justify-content:center;gap:8px;padding:16px;
  border-top:1px solid #2a2a2a;
}
.star-btn{
  width:52px;height:52px;border-radius:50%;border:2px solid #444;
  background:#1a1a1a;color:#aaa;font-size:18px;font-weight:700;
  cursor:pointer;transition:all .15s;
  display:flex;align-items:center;justify-content:center;
}
.star-btn:hover{transform:scale(1.1)}
.star-btn[data-score="1"]{border-color:#ef5350}
.star-btn[data-score="1"]:hover{background:#ef5350;color:#fff}
.star-btn[data-score="2"]{border-color:#ff9800}
.star-btn[data-score="2"]:hover{background:#ff9800;color:#fff}
.star-btn[data-score="3"]{border-color:#fdd835}
.star-btn[data-score="3"]:hover{background:#fdd835;color:#000}
.star-btn[data-score="4"]{border-color:#66bb6a}
.star-btn[data-score="4"]:hover{background:#66bb6a;color:#fff}
.star-btn[data-score="5"]{border-color:#4caf50}
.star-btn[data-score="5"]:hover{background:#4caf50;color:#fff}

.skip-btn{
  display:block;margin:0 auto 20px;background:none;border:1px solid #333;
  color:#666;border-radius:6px;padding:6px 20px;font-size:12px;cursor:pointer;
}
.skip-btn:hover{color:#aaa;border-color:#555}

/* -- Empty / done -- */
.done-msg{
  text-align:center;padding:40px;color:#4caf50;font-size:18px;font-weight:600;
}

/* -- Keyboard hint -- */
.hint{text-align:center;font-size:11px;color:#444;margin-top:12px}
</style>
</head>
<body>

<header>
  <h1>Peace<span>Grappler</span></h1>
  <nav>
    <a href="/builder">Builder</a>
    <a href="/analyze">Analyze</a>
    <a href="/library">Library</a>
    <a href="/wizard">AI Wizard</a>
    <a href="/rate" class="active">Rate</a>
  </nav>
</header>

<div class="content">
  <div class="controls">
    <label>Filter</label>
    <select id="tag-filter" onchange="loadScenes()">
      <option value="">All Scenes</option>
      <option value="__unrated__">Unrated Only</option>
    </select>
    <span class="progress-text" id="progress-text"></span>
  </div>

  <div id="card-area"></div>
  <div class="hint">Keyboard: 1-5 to rate, S to skip, R to include already-rated</div>
</div>

<script>
var scenes = [];
var currentIdx = 0;
var showRated = true;

async function loadScenes() {
  var tag = document.getElementById('tag-filter').value;
  var url = '/rate/api/scenes';
  if (tag && tag !== '__unrated__') {
    url += '?tag=' + encodeURIComponent(tag);
  }

  var data = await fetch(url).then(function(r){return r.json()});

  if (tag === '__unrated__') {
    scenes = data.filter(function(s) { return s.times_graded === 0; });
  } else {
    scenes = data;
  }

  // Shuffle for variety
  for (var i = scenes.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = scenes[i]; scenes[i] = scenes[j]; scenes[j] = tmp;
  }

  currentIdx = 0;
  renderCard();
}

function renderCard() {
  var area = document.getElementById('card-area');
  var prog = document.getElementById('progress-text');

  if (currentIdx >= scenes.length) {
    area.innerHTML = '<div class="done-msg">All scenes rated!</div>';
    prog.textContent = scenes.length + ' / ' + scenes.length;
    return;
  }

  var s = scenes[currentIdx];
  prog.textContent = (currentIdx + 1) + ' / ' + scenes.length;

  var prevGrade = s.avg_grade
    ? '<div class="prev-grade">Previously rated: ' + s.avg_grade + '/5 (' + s.times_graded + 'x)</div>'
    : '';

  var tags = s.tags.length ? s.tags.join(', ') : 'no tags';

  area.innerHTML = '<div class="rate-card">'
    + '<div class="thumb-wrap">'
    + '<img src="/api/thumbnail/' + s.id + '" loading="lazy"/>'
    + '</div>'
    + '<div class="meta">'
    + '<div class="filename">' + s.filename + ' [' + s.start.toFixed(1) + '-' + s.end.toFixed(1) + ']</div>'
    + '<div class="tags">' + tags + '</div>'
    + '<div class="dur">' + s.duration + 's' + (s.wide ? ' (wide)' : '') + '</div>'
    + prevGrade
    + '</div>'
    + '<div class="stars">'
    + '<button class="star-btn" data-score="1" onclick="rate(1)">1</button>'
    + '<button class="star-btn" data-score="2" onclick="rate(2)">2</button>'
    + '<button class="star-btn" data-score="3" onclick="rate(3)">3</button>'
    + '<button class="star-btn" data-score="4" onclick="rate(4)">4</button>'
    + '<button class="star-btn" data-score="5" onclick="rate(5)">5</button>'
    + '</div>'
    + '</div>'
    + '<button class="skip-btn" onclick="skip()">Skip</button>';
}

async function rate(score) {
  var s = scenes[currentIdx];
  await fetch('/rate/api/grade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scene_id: s.id, score: score}),
  });
  currentIdx++;
  renderCard();
}

function skip() {
  currentIdx++;
  renderCard();
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  var key = e.key;
  if (key >= '1' && key <= '5') {
    rate(parseInt(key));
  } else if (key === 's' || key === 'S') {
    skip();
  }
});

// Load tags into filter
async function loadTags() {
  var tags = await fetch('/rate/api/tags').then(function(r){return r.json()});
  var sel = document.getElementById('tag-filter');
  for (var tag in tags) {
    var o = document.createElement('option');
    o.value = tag;
    o.textContent = tag + ' (' + tags[tag] + ')';
    sel.appendChild(o);
  }
}

loadTags();
loadScenes();
</script>
</body>
</html>"""
