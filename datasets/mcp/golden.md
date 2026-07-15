# Golden Dataset (RAG prompt experiments)

Machine-parseable golden Q&A set for local Ollama instruct model prompt tuning
(`search_tools.generation_profiles.local` in `services/mcp_server/config.yml`):
5 hand-verified
questions with known-correct answers and the exact retrieved chunks pinned, so
prompt-structure changes can be compared without retrieval noise. Parse by
splitting on `## ` (query blocks) then `### ` (fields within a block).

## Q1

### Query
Что проверяет панель Validator в Unified Editor?

### Golden Answer
Validator проверяет объект на корректность соединений проводов (wire connections), имена текстур, корректность свойств материалов и т.п. Ошибки разной серьёзности показаны разными цветами (Inf/Wrn/Err).

### Chunks

#### Chunk 1
- title: Validator
- source_path: ue/user-interface/panels/validator/index.md
- updated: 2024-07-09
- retrieval_score: 0.758441

```text
Unified Editor — Validator

The `Validator` panel checks an object for correct wire connections, texture names, correct material properties, etc. Errors of different severity are represented with different colors.
```

#### Chunk 2
- title: Validator
- source_path: ue/user-interface/panels/validator/index.md
- updated: 2024-07-09
- retrieval_score: 0.6069838

```text
Unified Editor — Validator > General

No.
Validation technical details
Wrn.1
Reduce the number of triangles in the BSP model to 100.
Err.2
Check manually and assign a proper shader.
Skybox shader is only used for objects from \res\maps\skyboxes folder.
Inf.3
Save the model again and assess it visually to make sure that everything looks properly.
```

#### Chunk 3
- title: Validator
- source_path: ue/user-interface/panels/validator/index.md
- updated: 2024-07-09
- retrieval_score: 0.5891692

```text
Unified Editor — Validator > General

to draw attention.
Err.8
Assign proper texture in ModelEditor.
Inf.9
Object nodes tree varies in different LODs.
Err.10
Check manually if all textures have relative paths.
```

#### Chunk 4
- title: Maps Validator
- source_path: ue/editor-pages/space-editor/tools/maps-validator/index.md
- updated: None
- retrieval_score: 0.5598242

```text
Unified Editor — Maps Validator

The `Maps Validator` tool allows to validate the selected map with respect to the selected parameters.

Validate Trees option allows to check trees registration; Validate Models option finds missing textures etc.
```

#### Chunk 5
- title: Validator
- source_path: ue/user-interface/panels/validator/index.md
- updated: 2024-07-09
- retrieval_score: 0.55933183

```text
Unified Editor — Validator > Objects from Content Folder

Wrn.135 The BSP in LOD0 should not be different from the BSP in LOD1, LOD2, etc.
```

## Q2

### Query
Какие режимы фона доступны в панели Background?

### Golden Answer
No Background, Floor Texture, Terrain (и дополнительно фон для гусениц/tracks в tank editor).

### Chunks

#### Chunk 1
- title: Background
- source_path: ue/user-interface/viewport/background/index.md
- updated: 2024-07-03
- retrieval_score: 0.5910203

```text
Unified Editor — Background

The `Background` menu allows us to change the appearance of the background where the model is previewed.

**No Background**
Remove any background. We can also choose the background color while in this mode.
**Floor Texture**
We can choose any texture as our background. By default, the movement grid is selected.
**Terrain**
We can also choose the terrain option.

You can also choose a background for tracks.
```

#### Chunk 2
- title: Heatmaps
- source_path: ue/editor-pages/space-editor/tools/heatmaps/index.md
- updated: None
- retrieval_score: 0.41535905

```text
Unified Editor — Heatmaps > Tool Options

Heatmap display modes are located in the `General` section.
```

#### Chunk 3
- title: Quality
- source_path: ue/user-interface/viewport/quality/index.md
- updated: None
- retrieval_score: 0.41258758

```text
Unified Editor — Quality > Graphics Settings

This panel allows us to customize individual graphics settings. Selected settings are shown in blue.
```

#### Chunk 4
- title: Panels
- source_path: engineering/documentation/shortcodes/panel/_index.md
- updated: None
- retrieval_score: 0.40022153

```text
Engineering — Panels > theme

Suspendisse potenti. Sed nec magna ut quam facilisis commodo eget eget urna. (theme=primary/notice/success/warning/danger/purple)
```

#### Chunk 5
- title: Panels
- source_path: engineering/documentation/cookbook/panel/_index.md
- updated: None
- retrieval_score: 0.39784992

```text
Engineering — Panels
Content focus and highlight

Lorem ipsum dolor sit amet, consectetur adipiscing elit.
```

## Q3

### Query
Where was the Distortion Amount slider moved to in the Particle Tool update?

### Golden Answer
Distortion Amount was moved to the lighting section; the Distortion section and its slider were removed.

### Chunks

#### Chunk 1
- title: Grid Re-Arrange
- source_path: ue/editor-pages/space-editor/tools/particle-tool/grid-re-arrange/index.md
- updated: 2024-04-25
- retrieval_score: 0.57468957

```text
Unified Editor — Grid Re-Arrange > Distortion Amount

* `Distortion Amount` moved to lighting section
* `Distortion` section removed
* Slider for `Distortion Amount` also removed
```

#### Chunk 2
- title: GPU Particles Editor
- source_path: updates/11_gpu_particles/index.md
- updated: None
- retrieval_score: 0.5521831

```text
What's New — GPU Particles Editor > Tool Options Filter

The `Tool Options` window of `Particle Mode` in `Space Editor` now has a `CPU`/`GPU` checkbox filter.
```

#### Chunk 3
- title: GPU Particles Editor
- source_path: updates/11_gpu_particles/index.md
- updated: None
- retrieval_score: 0.53111833

```text
What's New — GPU Particles Editor > GPU Section Hierarchy

The `Tool Options` now has a hierarchy displaying all the parameters from the Property Grid.
```

#### Chunk 4
- title: Particle Tool
- source_path: ue/editor-pages/space-editor/tools/particle-tool/_index.md
- updated: None
- retrieval_score: 0.524386

```text
Unified Editor — Particle Tool

The `Particle Tool` allows us to edit the properties of particle systems. To open, select the Particle Tool from the toolbar.
```

#### Chunk 5
- title: GPU Particles Editor
- source_path: updates/11_gpu_particles/index.md
- updated: None
- retrieval_score: 0.5217072

```text
What's New — GPU Particles Editor > Curve Editor

The `Edit Curves` button in the Property Grid of GPU Particles is moved to the top of the editor.
```

## Q4

### Query
Как удалить кость в режиме Inverse Kinematics?

### Golden Answer
Выбрать кость во Viewport или Scene Browser и нажать кнопку delete. Все кости ниже выбранной в иерархии будут удалены.

### Chunks

#### Chunk 1
- title: Inverse Kinematics Mode
- source_path: ue/editor-pages/model-editor/tools/inverse-kinematics-mode/index.md
- updated: 2025-09-12
- retrieval_score: 0.6559917

```text
Unified Editor — Inverse Kinematics Mode > Adding and Removing Bones to an Avatar

Adding and deleting is recursive.

To delete, select a bone in the Viewport or Scene Browser and click the delete button.

All bones below the selected one in the hierarchy will be deleted.

To add a bone, press the `+` button.
```

#### Chunk 2
- title: Inverse Kinematics Mode
- source_path: ue/editor-pages/model-editor/tools/inverse-kinematics-mode/index.md
- updated: 2025-09-12
- retrieval_score: 0.59682935

```text
Unified Editor — Inverse Kinematics Mode > Tool Use

To select the tool, click on the `Inverse Kinematics Mode` icon in the toolbar. A hierarchy is then displayed in the Scene Browser. Each sphere corresponds to a bone of the skeleton.
```

#### Chunk 3
- title: Inverse Kinematics Mode
- source_path: ue/editor-pages/model-editor/tools/inverse-kinematics-mode/index.md
- updated: 2025-09-12
- retrieval_score: 0.5792351

```text
Unified Editor — Inverse Kinematics Mode > Options

Enabled: Disable selection, spheres become invisible. Sphere Radius. Desired Position Sphere: Target Position Radius. Aiming To Desired Position: bones move towards Target Position.
```

#### Chunk 4
- title: Inverse Kinematics Mode
- source_path: ue/editor-pages/model-editor/tools/inverse-kinematics-mode/index.md
- updated: 2025-09-12
- retrieval_score: 0.54594594

```text
Unified Editor — Inverse Kinematics Mode > Modifying Bone Constraints

First, select the skeleton bone to which the avatar bone is assigned. Switching to Property Grid we can see the following parameters.
```

#### Chunk 5
- title: Inverse Kinematics Mode
- source_path: ue/editor-pages/model-editor/tools/inverse-kinematics-mode/index.md
- updated: 2025-09-12
- retrieval_score: 0.5363988

```text
Unified Editor — Inverse Kinematics Mode

The `Inverse Kinematics Mode` allows users to set animation constraints, such that the animated object would move in an expected way.
```

## Q5

### Query
Что было исправлено с Orbit Camera в патче v1.23.1?

### Golden Answer
Движение камеры больше не прерывается при использовании клавиш для перемещения во время удержания ПКМ (RMB).

### Chunks

#### Chunk 1
- title: v1.23.1 Bugfix
- source_path: updates/7.md
- updated: 2024-01-08
- retrieval_score: 0.7098931

```text
What's New — v1.23.1 Bugfix > Fixed the Orbit Camera while holding RMB

The camera movement is no longer interrupted when using keys to move while holding RMB
```

#### Chunk 2
- title: v1.23.1 Bugfix
- source_path: updates/7.md
- updated: 2024-01-08
- retrieval_score: 0.5559974

```text
What's New — v1.23.1 Bugfix > Fixed the 'plg_space_editor crash'

Tinkering with particle components of Child Game Objects no longer results in a crash
```

#### Chunk 3
- title: v1.22.1 Bugfix
- source_path: updates/5.md
- updated: None
- retrieval_score: 0.54433197

```text
What's New — v1.22.1 Bugfix > Fixed the 'ASSERTION FAILED: radius > 0' crash

Moving the orthogonal camera no longer results in a crash
```

#### Chunk 4
- title: v1.23.1 Bugfix
- source_path: updates/7.md
- updated: 2024-01-08
- retrieval_score: 0.5271621

```text
What's New — v1.23.1 Bugfix > Fixed the Crash state for tanks

The viewport now properly displays the model of the destroyed tank according to the selected set in Model Sets
```

#### Chunk 5
- title: v1.23.1 Bugfix
- source_path: updates/7.md
- updated: 2024-01-08
- retrieval_score: 0.5159853

```text
What's New — v1.23.1 Bugfix > Fixed road clone controller crash

Selecting then deselecting road icons no longer results in a crash
```
