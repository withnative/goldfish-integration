-- Synthetic Goldfish fixture: real Goldfish schema (indexes and FTS triggers omitted), invented content.
-- Flags 10/13/15 and 12/14 are reworded duplicates; 1001 (work) and 1002 (family) are WhatsApp
-- flags with identical metadata. created_at / captured_at are Unix SECONDS.

CREATE TABLE attention_items (
        item_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('email','event','commitment')),
        title TEXT NOT NULL,
        participants TEXT NOT NULL,
        summary TEXT NOT NULL,
        evidence TEXT NOT NULL,
        starts_at INTEGER,
        ends_at INTEGER,
        status TEXT NOT NULL CHECK(status IN ('active','done','cancelled','tentative')),
        source_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
        observed_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    , recurrence TEXT NOT NULL DEFAULT '', event_kind TEXT NOT NULL DEFAULT '', wall_clock TEXT NOT NULL DEFAULT '', zone_offset_s INTEGER, zone_source TEXT NOT NULL DEFAULT '', calendars TEXT NOT NULL DEFAULT '');
CREATE TABLE salient_flags (
        flag_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT    NOT NULL,
        text         TEXT    NOT NULL,
        snapshot_id  TEXT,
        period_key   TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'open',
        created_at   INTEGER NOT NULL
    );
INSERT INTO "salient_flags" VALUES(1,'QUESTION','Asked how the example feature 1 works.',NULL,'chat-2026-11-10T09','open',1794304562);
INSERT INTO "salient_flags" VALUES(2,'QUESTION','Asked how the example feature 2 works.',NULL,'chat-2026-11-10T09','open',1794304562);
INSERT INTO "salient_flags" VALUES(3,'BLOCKER','Example app 3 failed to load.','be57b4c0e6748b127db1e909e888b0e8acfff4c4fd7bd933ccd024cfe5a32fe8','2026-11-10T09','open',1794307449);
INSERT INTO "salient_flags" VALUES(4,'BLOCKER','Example app 4 failed to load.','eab82a99a70c0f61cbc2ef2ec80665145f7f85946aa9432c380bfdfa57f63441','2026-11-10T09','open',1794307449);
INSERT INTO "salient_flags" VALUES(5,'QUESTION','Asked how the example feature 5 works.',NULL,'chat-2026-11-10T10','open',1794308336);
INSERT INTO "salient_flags" VALUES(6,'QUESTION','Asked how the example feature 6 works.',NULL,'chat-2026-11-10T11','open',1794309536);
INSERT INTO "salient_flags" VALUES(7,'QUESTION','Asked how the example feature 7 works.',NULL,'chat-2026-11-10T11','open',1794309836);
INSERT INTO "salient_flags" VALUES(8,'QUESTION','Asked how the example feature 8 works.',NULL,'chat-2026-11-10T11','open',1794310736);
INSERT INTO "salient_flags" VALUES(9,'QUESTION','Asked how the example feature 9 works.',NULL,'chat-2026-11-10T11','open',1794310736);
INSERT INTO "salient_flags" VALUES(10,'BLOCKER','Could not create a document in Docs; the workspace was still loading.','dae67da4e16569a875626fbcd81c26b389173bf687059bf27cdd67e8e1dc5081','2026-11-10T11','open',1794312072);
INSERT INTO "salient_flags" VALUES(11,'BLOCKER','Example app 11 failed to load.','d35bd4cba8f3fe931838a234e1a5107590609715812a27620e7125fddaab4083','2026-11-10T11','open',1794312072);
INSERT INTO "salient_flags" VALUES(12,'ARTIFACT','PR #101, the onboarding checklist change, merged.','fe71c54ba7a0fea3b502b35bdd6f49243183cdaf1058bdc3abc71a421b12396d','2026-11-10T11','open',1794312947);
INSERT INTO "salient_flags" VALUES(13,'BLOCKER','Docs refused to create a document while the workspace finished loading.','dae67da4e16569a875626fbcd81c26b389173bf687059bf27cdd67e8e1dc5081','2026-11-10T11','open',1794312947);
INSERT INTO "salient_flags" VALUES(14,'ARTIFACT','PR101 (onboarding checklist) merged.','fe71c54ba7a0fea3b502b35bdd6f49243183cdaf1058bdc3abc71a421b12396d','2026-11-10T11','open',1794312993);
INSERT INTO "salient_flags" VALUES(15,'BLOCKER','Creating a document in Docs failed until the workspace had loaded.','dae67da4e16569a875626fbcd81c26b389173bf687059bf27cdd67e8e1dc5081','2026-11-10T11','open',1794312993);
INSERT INTO "salient_flags" VALUES(16,'BLOCKER','Example app 16 failed to load.','d35bd4cba8f3fe931838a234e1a5107590609715812a27620e7125fddaab4083','2026-11-10T11','open',1794312993);
INSERT INTO "salient_flags" VALUES(17,'BLOCKER','Example app 17 failed to load.','922d65b35c33616fbab3f1061ff9734bde7ff786433a0a86b62be07d7088d816','2026-11-10T12','open',1794315634);
INSERT INTO "salient_flags" VALUES(18,'QUESTION','Asked how the example feature 18 works.','4cfd63eccc4aae3388b8705807a407d510eca5e210da3d342679870a05ed8dd1','2026-11-10T12','open',1794315634);
INSERT INTO "salient_flags" VALUES(19,'QUESTION','Asked how the example feature 19 works.',NULL,'chat-2026-11-10T13','open',1794318019);
INSERT INTO "salient_flags" VALUES(20,'ARTIFACT','Created example artifact 20.','505990fb6d35e8ad9a104c6e7492d732f5e033bc86fe0f8c6eb96ba3964c30b8','2026-11-10T13','open',1794319234);
INSERT INTO "salient_flags" VALUES(21,'BLOCKER','Example app 21 failed to load.','9bc9a4f2ff5e3b2d7f968f44e1ef7e093056a22f363741faad57b73a005378f6','2026-11-10T13','open',1794319234);
INSERT INTO "salient_flags" VALUES(22,'QUESTION','Asked how the example feature 22 works.','eb777e9ae8296357ccf25666fe1f35a79f49682b7ba5c56b127bb1c16384deee','2026-11-10T13','open',1794319234);
INSERT INTO "salient_flags" VALUES(23,'ARTIFACT','Created example artifact 23.','3787a03f495763cf62d08d24d9c6522870063445e94defaa327ffe7347825455','2026-11-10T13','open',1794319234);
INSERT INTO "salient_flags" VALUES(24,'BLOCKER','Example app 24 failed to load.','6cc0ee8b19fecb553906f8e29bd2241e573cfa2667ef55f7ad877d1efeb602f1','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(25,'BLOCKER','Example app 25 failed to load.','c00859e76198f9dcb796889b6aa76819fe985ed220f5e4a2ba42c82e8352f5fb','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(26,'BLOCKER','Example app 26 failed to load.','8a83d94d44b74afe4f81305048bf6e5b3f5f5ba22a2947652fdcf5b83290fc8c','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(27,'ARTIFACT','Created example artifact 27.','a4b051b4b36c775202520039064c6c50aca0156b7705b4dcc816da9b1aec36ae','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(28,'ARTIFACT','Created example artifact 28.','8f2111b1143b1162a00e824fe7bb57930747b5a8f25dc8e7d51ffaf8e4d55382','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(29,'ARTIFACT','Created example artifact 29.','8b862b94412c99183f38cf5203e7597c58723112c83ebd788d934f0533ea3f58','2026-11-10T14','open',1794322829);
INSERT INTO "salient_flags" VALUES(30,'BLOCKER','Example app 30 failed to load.','aa2f4fa70b74a4b8851e3247959596141665da2a6bb16b4d52db4fc81653a707','2026-11-10T15','open',1794326435);
INSERT INTO "salient_flags" VALUES(31,'QUESTION','Asked how the example feature 31 works.','aa2f4fa70b74a4b8851e3247959596141665da2a6bb16b4d52db4fc81653a707','2026-11-10T15','open',1794326435);
INSERT INTO "salient_flags" VALUES(32,'BLOCKER','Example app 32 failed to load.','47860669af441a1736781d130368503e133945d59e16874f0cec0d461429f6db','2026-11-10T15','open',1794326435);
INSERT INTO "salient_flags" VALUES(1001,'COMMITMENT','SYNTHETIC: Send the client the demo link before Thursday.','synthetic-wa-work','2026-11-10T13','open',1794318100);
INSERT INTO "salient_flags" VALUES(1002,'COMMITMENT','SYNTHETIC: Call Mum on Sunday about the visit.','synthetic-wa-family','2026-11-10T13','open',1794318100);
CREATE TABLE snapshots (
        snapshot_id    TEXT PRIMARY KEY,
        content        TEXT NOT NULL,
        app            TEXT NOT NULL,
        window_title   TEXT NOT NULL,
        url            TEXT,
        domain         TEXT,
        captured_at    INTEGER NOT NULL,
        last_seen_at   INTEGER NOT NULL,
        duration_s     REAL    NOT NULL DEFAULT 0,
        sighting_count INTEGER NOT NULL DEFAULT 1,
        schema_v       INTEGER NOT NULL DEFAULT 1
    , focused_name TEXT, focused_role TEXT, focused_path TEXT, body TEXT, body_v INTEGER NOT NULL DEFAULT 0);
INSERT INTO "snapshots" VALUES('dae67da4e16569a875626fbcd81c26b389173bf687059bf27cdd67e8e1dc5081','','native desktop','Example window','https://app.withnative.ai/example','app.withnative.ai',1794309594,1794309594,0.0,1,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('8a83d94d44b74afe4f81305048bf6e5b3f5f5ba22a2947652fdcf5b83290fc8c','','native desktop','Example window','https://paseo:/example','paseo:',1794321610,1794322776,1.322954491259999657e+02,19,62,NULL,'Button',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('aa2f4fa70b74a4b8851e3247959596141665da2a6bb16b4d52db4fc81653a707','','native desktop','Example window','https://paseo:/example','paseo:',1794323787,1794323845,3.382765750100001157e+01,3,62,NULL,'WebArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('4cfd63eccc4aae3388b8705807a407d510eca5e210da3d342679870a05ed8dd1','','native desktop','Example window','https://paseo:/example','paseo:',1794315266,1794315277,8.107316083,2,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('9bc9a4f2ff5e3b2d7f968f44e1ef7e093056a22f363741faad57b73a005378f6','','native desktop','Example window','https://paseo:/example','paseo:',1794316518,1794316538,20.70852525,1,62,NULL,'WebArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('505990fb6d35e8ad9a104c6e7492d732f5e033bc86fe0f8c6eb96ba3964c30b8','','native desktop','Example window','https://paseo:/example','paseo:',1794316461,1794316919,1.906179504190000386e+02,12,62,NULL,NULL,NULL,NULL,1);
INSERT INTO "snapshots" VALUES('eb777e9ae8296357ccf25666fe1f35a79f49682b7ba5c56b127bb1c16384deee','','chatgpt','Example window','https://app:/example','app:',1794316939,1794316944,5.155407332999999426e+00,1,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('be57b4c0e6748b127db1e909e888b0e8acfff4c4fd7bd933ccd024cfe5a32fe8','','native desktop','Example window','https://app.withnative.ai/example','app.withnative.ai',1794304631,1794304633,1.327596583,1,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('8b862b94412c99183f38cf5203e7597c58723112c83ebd788d934f0533ea3f58','','native desktop','Example window','https://paseo:/example','paseo:',1794320852,1794321007,8.08291128760000106e+01,5,62,NULL,'TextField',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('922d65b35c33616fbab3f1061ff9734bde7ff786433a0a86b62be07d7088d816','','native desktop','Example window','https://app.withnative.ai/example','app.withnative.ai',1794313380,1794313382,2.598506667,1,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('8f2111b1143b1162a00e824fe7bb57930747b5a8f25dc8e7d51ffaf8e4d55382','','native desktop','Example window','https://paseo:/example','paseo:',1794321015,1794321605,70.274683794,9,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('3787a03f495763cf62d08d24d9c6522870063445e94defaa327ffe7347825455','','native desktop','Example window','https://paseo:/example','paseo:',1794316873,1794316876,2.485905833,1,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('eab82a99a70c0f61cbc2ef2ec80665145f7f85946aa9432c380bfdfa57f63441','','native desktop','Example window','https://app.withnative.ai/example','app.withnative.ai',1794304635,1794304715,1.734749625100000258e+01,4,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('6cc0ee8b19fecb553906f8e29bd2241e573cfa2667ef55f7ad877d1efeb602f1','','native desktop','Example window','https://paseo:/example','paseo:',1794320140,1794320140,0.0,1,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('d35bd4cba8f3fe931838a234e1a5107590609715812a27620e7125fddaab4083','','native desktop','Example window','https://app.withnative.ai/example','app.withnative.ai',1794309594,1794309594,0.0,1,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('c00859e76198f9dcb796889b6aa76819fe985ed220f5e4a2ba42c82e8352f5fb','','native desktop','Example window','https://paseo:/example','paseo:',1794320088,1794320392,41.645798625,10,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('fe71c54ba7a0fea3b502b35bdd6f49243183cdaf1058bdc3abc71a421b12396d','','native desktop','Example window','https://paseo:/example','paseo:',1794308418,1794308585,7.866092917,3,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('47860669af441a1736781d130368503e133945d59e16874f0cec0d461429f6db','','comet','Example window','https://app.withnative.ai/example','app.withnative.ai',1794324018,1794324030,6.804235167,2,62,NULL,'WebArea',NULL,NULL,-3);
INSERT INTO "snapshots" VALUES('a4b051b4b36c775202520039064c6c50aca0156b7705b4dcc816da9b1aec36ae','','native desktop','Example window','https://paseo:/example','paseo:',1794320922,1794322665,153.240330208,34,62,NULL,'TextArea',NULL,NULL,1);
INSERT INTO "snapshots" VALUES('synthetic-wa-work','','whatsapp','Example window',NULL,NULL,1794318000,1794318000,30.0,1,1,NULL,'Group',NULL,NULL,0);
INSERT INTO "snapshots" VALUES('synthetic-wa-family','','whatsapp','Example window',NULL,NULL,1794318060,1794318060,30.0,1,1,NULL,'Group',NULL,NULL,0);
CREATE TABLE wiki_facts (
        fact_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        slug               TEXT    NOT NULL,
        fact               TEXT    NOT NULL,
        as_of              TEXT,
        source_snapshot_id TEXT,
        created_at         INTEGER NOT NULL,
        last_confirmed_at  INTEGER NOT NULL,
        invalid_at         INTEGER
    , importance REAL);
