// Server-side @whop/sdk bridge.
//
// Reads a single JSON request object from stdin and writes a single JSON
// response object to stdout. The Python WhopPublisher invokes this via a
// subprocess so uploads always go through the official @whop/sdk.
//
// Flow (per https://docs.whop.com/developer/guides/upload-files):
//   1. files.upload() creates the record, uploads to the presigned URL, and
//      polls until the file is "ready", returning the file id + final url.
//   2. The returned file id is attached to a chat message, forum post, or
//      course lesson depending on target_type. Products do not accept file
//      attachments, so those uploads are returned for manual placement.
import fs from "node:fs";
import Whop from "@whop/sdk";

function fail(message) {
  process.stdout.write(JSON.stringify({ success: false, error: message }));
  process.exit(0);
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const apiKey = process.env.WHOP_API_KEY;
  if (!apiKey) fail("WHOP_API_KEY is not set");

  const client = new Whop({ apiKey });

  // 1. Upload the clip. The SDK handles create -> presigned upload -> poll.
  const uploaded = await client.files.upload(
    fs.createReadStream(input.video_path),
    { filename: input.filename }
  );

  const fileId = uploaded.id;
  const caption = input.caption || input.title || "";
  const attachments = [{ id: fileId }];
  let attached = false;

  // 2. Attach to a supported destination when a target is provided.
  const targetType = input.target_type || "";
  const targetId = input.target_id || "";

  if (targetId) {
    if (targetType === "chat") {
      await client.messages.create({
        channel_id: targetId,
        content: caption,
        attachments,
      });
      attached = true;
    } else if (targetType === "forum") {
      await client.forumPosts.create({
        experience_id: targetId,
        title: input.title || undefined,
        content: caption,
        attachments,
      });
      attached = true;
    } else if (targetType === "course") {
      // Create a lesson in the target chapter, then attach the uploaded file.
      const lesson = await client.courseLessons.create({
        chapter_id: targetId,
        lesson_type: "text",
        title: input.title || undefined,
        content: caption,
      });
      await client.courseLessons.update(lesson.id, { attachments });
      attached = true;
    }
    // target_type === "product": products expose no file-attachment field on
    // the API, so the upload is returned as-is for manual placement.
  }

  process.stdout.write(
    JSON.stringify({
      success: true,
      file_id: fileId,
      url: uploaded.url || "",
      upload_status: uploaded.upload_status || "",
      attached,
      target_type: targetType,
      target_id: targetId,
    })
  );
}

main().catch((error) => fail(error?.message || String(error)));
