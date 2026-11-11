/** Normative boundaries for CSDC ES-module implementation. Not runtime code.
 * DTO definitions are closed schemas in contracts-v3.schema.json.
 * Verified/Auth/Permit values are minted by trusted factories, never caller booleans.
 */
export type Json = null | boolean | number | string | Json[] | {[key:string]: Json};
export type DTO = Readonly<Record<string, Json>>;
export interface AuthenticatedDevice { readonly __brand: 'device-auth'; readonly scope: DTO; }
export interface AuthenticatedOperator { readonly __brand: 'operator-auth'; }
export interface PublisherIdentity { readonly __brand: 'publisher-oidc'; }
export interface GlobalRolloutPermit { readonly __brand: 'global-rollout-permit'; }
export interface VerifiedRelease { readonly __brand: 'verified-release'; }
export interface PreparedStart { readonly __brand: 'prepared-start'; }
export interface Clock { nowMs():number; }
export interface DigestSink { write(part:Uint8Array):Promise<void>; close():Promise<string>; abort():Promise<void>; }
export interface ArtifactStore {
  putChunkCreateOnly(input:DTO, bytes:ReadableStream<Uint8Array>, deadlineMs:number):Promise<DTO>;
  readChunk(input:DTO, deadlineMs:number):Promise<ReadableStream<Uint8Array>>;
}
export interface Repo {
  /** Uses current DB auth/realm/revision/slot guards in one D1 batch. */
  conditionalCommit(intent:DTO, writes:ReadonlyArray<DTO>):Promise<DTO>;
  readKnownReceipt(scope:DTO, id:string):Promise<DTO|null>;
}
export interface Signer {
  /** Accepts allowlisted typed purpose+payload, not arbitrary external bytes. */
  signTyped(purpose:string, payload:DTO, deadlineMs:number):Promise<Uint8Array>;
}
export declare function parseTyped(raw:Uint8Array, definition:string, maxBytes:number):DTO;
export declare function canonicalTyped(value:DTO, definition:string):Uint8Array;
export declare function verifyPublisher(request:Request, expectedPolicy:DTO, nowMs:number):Promise<PublisherIdentity>;
export declare function requireRolloutOperator(identity:AuthenticatedOperator, action:string, repo:Repo):Promise<GlobalRolloutPermit>;
export declare function reservePublication(input:DTO, identity:PublisherIdentity, repo:Repo):Promise<DTO>;
export declare function fixObjectManifest(input:DTO, identity:PublisherIdentity, repo:Repo):Promise<DTO>;
export declare function verifyObject(input:DTO, identity:PublisherIdentity, store:ArtifactStore, repo:Repo, digest:()=>DigestSink):Promise<DTO>;
export declare function finalizePublication(input:DTO, identity:PublisherIdentity, repo:Repo, signer:Signer):Promise<DTO>;
export declare function registerPublication(input:DTO, identity:PublisherIdentity, repo:Repo):Promise<VerifiedRelease>;
export declare function confirmStandingConsent(input:DTO, auth:AuthenticatedDevice, repo:Repo, signer:Signer):Promise<DTO>;
export declare function revokeStandingConsent(input:DTO, auth:AuthenticatedDevice, repo:Repo, signer:Signer):Promise<DTO>;
export declare function createRollout(input:DTO, permit:GlobalRolloutPermit, repo:Repo):Promise<DTO>;
export declare function freezeCohort(rolloutId:string, permit:GlobalRolloutPermit, repo:Repo):Promise<DTO>;
export declare function rankFrozenTenants(snapshot:DTO, key:CryptoKey):Promise<DTO>;
export declare function stageTenantCap(stage:string, customerCount:number, pilotCount:number):number;
export declare function nextEligibleAssignments(rollout:DTO, snapshot:DTO, currentFacts:DTO):readonly DTO[];
export declare function selectCandidate(input:DTO, auth:AuthenticatedDevice, repo:Repo, signer:Signer):Promise<DTO>;
export declare function reserveStart(input:DTO, auth:AuthenticatedDevice, repo:Repo):Promise<PreparedStart>;
export declare function signReservedStart(prepared:PreparedStart, signer:Signer):Promise<Uint8Array>;
export declare function commitSignedStart(prepared:PreparedStart, signed:Uint8Array, auth:AuthenticatedDevice, repo:Repo):Promise<Uint8Array>;
export declare function cancelUnstarted(input:DTO, auth:AuthenticatedDevice, repo:Repo):Promise<DTO>;
export declare function acceptOutcome(raw:Uint8Array, auth:AuthenticatedDevice, repo:Repo, signer:Signer):Promise<DTO>;
export declare function acceptHealth(raw:Uint8Array, auth:AuthenticatedDevice, repo:Repo, signer:Signer):Promise<DTO>;
export declare function evaluatePromotion(rollout:DTO, snapshot:DTO, outcomes:readonly DTO[], health:readonly DTO[], nowMs:number):DTO;
export declare function previewRolloutAction(input:DTO, permit:GlobalRolloutPermit, repo:Repo):Promise<DTO>;
export declare function commitRolloutAction(input:DTO, permit:GlobalRolloutPermit, repo:Repo):Promise<DTO>;
export declare function autoPause(rolloutId:string, evidence:DTO, repo:Repo):Promise<DTO>;
export declare function evaluateRollouts(cursor:DTO|null, repo:Repo, budget:DTO):Promise<DTO>;

export declare function proposeEnrollment(input:DTO, identity:AuthenticatedOperator, repo:Repo):Promise<DTO>;
