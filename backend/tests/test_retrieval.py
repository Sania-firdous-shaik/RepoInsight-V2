import unittest
from types import SimpleNamespace

from app.services.retrieval.query_analysis import analyze_query
from app.services.retrieval.pipeline import (filter_repository_points, lexical_rank, merge_rankings,
                                               discover_architecture_hits, discover_import_related_hits,
                                               discover_model_coverage_hits, discover_source_role_hits,
                                               prioritize_workflow_roles, retrieve_results, scroll_all_pages, select_diverse)
from app.services.retrieval.context_builder import build_context, estimate_tokens
from app.services.retrieval.query_analysis import documentation_retrieval_query
from app.services.retrieval.workflow_context import (authentication_workflow_index_inventory,
    authentication_workflow_stage_requirements, business_workflow_stage_requirements,
    format_authentication_workflow_inventory,
    missing_authentication_workflow_evidence)

def point(id,path,symbol,content,start=1,end=10,repo=1,generation=None,digest=None):
    payload={'repository_id':repo,'index_generation':generation,'file_path':path,'symbol_name':symbol,
             'symbol_type':'function','content':content,'start_line':start,'end_line':end,
             'content_hash':digest or f'{path}:{symbol}:{start}'}
    return SimpleNamespace(id=id,payload=payload)

class RetrievalTests(unittest.TestCase):
    def test_query_analysis_extracts_paths_identifiers_endpoints_and_workflow(self):
        q=analyze_query('Explain complete workflow for handlePlaceOrder at POST /api/orders using Checkout.jsx')
        self.assertIn('handlePlaceOrder',q.identifiers)
        self.assertIn('Checkout.jsx',q.file_paths)
        self.assertIn('POST /api/orders',q.endpoints)
        self.assertTrue(q.complex)
        self.assertIn('workflow',q.intents)

    def test_authentication_flow_is_a_multi_file_workflow_intent(self):
        q=analyze_query('Explain the complete Hastakala authentication flow from login to JWT and protected frontend routes')
        self.assertIn('authentication',q.intents)
        self.assertIn('authentication_workflow',q.intents)
        self.assertTrue(q.complex)
        end_to_end=analyze_query('Trace authentication end-to-end, including verification and activation')
        self.assertIn('authentication_workflow',end_to_end.intents)

    def test_query_expansion_is_intent_scoped_and_mixed_business_workflow_is_not_auth_only(self):
        api=analyze_query('What happens when POST /api/orders is called and how is the response validated?')
        self.assertIn('api',api.intents)
        self.assertTrue({'handler','request','response','validation'} & set(api.expanded_terms))
        debug=analyze_query('Why might payment verification not work?')
        self.assertIn('debugging',debug.intents)
        self.assertTrue({'exception','error handling','validation'} & set(debug.expanded_terms))
        mixed=analyze_query('Explain the complete order and payment workflow, including authentication middleware')
        self.assertIn('workflow',mixed.intents)
        self.assertNotIn('authentication_workflow',mixed.intents)

    def test_documentation_types_have_distinct_queries_and_limits(self):
        profiles={kind:documentation_retrieval_query(kind,'owner/repo') for kind in ('overview','readme','api','architecture','module')}
        self.assertEqual(len({query for query,_ in profiles.values()}),5)
        self.assertIn('installation',profiles['readme'][0])
        self.assertIn('HTTP methods',profiles['api'][0])
        self.assertIn('imports',profiles['architecture'][0])
        self.assertGreater(profiles['api'][1],profiles['readme'][1])

    def test_exact_filename_and_symbol_rank_above_generic_documentation(self):
        query=analyze_query('Explain handlePlaceOrder in craft-app-frontend/src/pages/Checkout.jsx')
        code=point('c','craft-app-frontend/src/pages/Checkout.jsx','handlePlaceOrder','function handlePlaceOrder(){ api.post("/api/orders") }')
        readme=point('r','README.md','README','This project allows placing an order')
        ranked=lexical_rank([readme,code],query)
        self.assertEqual(ranked[0].id,'c')

    def test_exact_endpoint_match(self):
        query=analyze_query('What does POST /api/payments/verify do?')
        route=point('route','routes/payments.js','POST /api/payments/verify','router.post("/api/payments/verify", verifySignature)')
        other=point('other','README.md','readme','payment integrations')
        self.assertEqual(lexical_rank([other,route],query)[0].id,'route')

    def test_api_retrieval_prefers_route_metadata_and_limits_readme_for_code_questions(self):
        query=analyze_query('Explain the POST /api/orders route implementation')
        route=point('route','server/routes/orders.js','POST /api/orders',"router.post('/api/orders', validate, createOrder)")
        route.payload['symbol_type']='route'
        readme_a=point('readme-a','README.md','README','POST /api/orders creates a new order with request validation')
        readme_b=point('readme-b','server/README.md','README','POST /api/orders creates a new order with request validation')
        hits=merge_rankings([readme_a,readme_b],[route,readme_a,readme_b],query)
        selected=select_diverse(hits,query,8)
        self.assertEqual(selected[0].payload['file_path'],'server/routes/orders.js')
        self.assertLessEqual(sum(hit.payload['file_path'].endswith('.md') for hit in selected),1)

    def test_readme_question_can_promote_documentation_sources(self):
        query=analyze_query('Find README installation and setup instructions')
        readme=point('readme','README.md','README','Installation: npm install; setup with npm run dev')
        code=point('code','src/main.js','main','function main(){ startServer() }')
        ranked=merge_rankings([code,readme],[code,readme],query)
        self.assertEqual(ranked[0].payload['file_path'],'README.md')

    def test_readme_documentation_retrieval_preserves_implementation_evidence(self):
        query=analyze_query('README documentation installation setup configuration environment variables usage scripts dependencies source-code implementation')
        points=[point('readme1','README.md','README','Installation and setup instructions'),
                point('readme2','backend/README.md','README','Backend setup instructions'),
                point('readme3','frontend/README.md','README','Frontend setup instructions'),
                point('route','backend/routes/users.js','users','router.post("/users", createUser)')]
        results=select_diverse(merge_rankings(points,points,query),query,8)
        self.assertLessEqual(sum(hit.payload['file_path'].lower().endswith('.md') for hit in results),2)
        self.assertTrue(any(not hit.payload['file_path'].lower().endswith('.md') for hit in results))

    def test_documentation_profiles_reserve_routes_and_architecture_entrypoints(self):
        api_query, _=documentation_retrieval_query('api')
        api_profile=analyze_query(api_query)
        route=point('route','server/routes/accounts.js','accounts','router.post("/accounts", createAccount)')
        route.payload['symbol_type']='route'
        component=point('component','web/src/components/Account.jsx','Account','fetch("/accounts")')
        api_hits=retrieve_results([component],[component,route],api_profile,limit=8)
        self.assertTrue(any(hit.payload['file_path']=='server/routes/accounts.js' for hit in api_hits))

        architecture_query, _=documentation_retrieval_query('architecture')
        architecture_profile=analyze_query(architecture_query)
        entry=point('entry','src/index.js','index','import App from "./App"; createRoot().render(<App />)')
        manifest=point('manifest','package.json','package','{"scripts":{"start":"node src/index.js"}}')
        service=point('service','src/services/accounts.js','accounts','export async function loadAccounts() {}')
        architecture_hits=retrieve_results([service],[service,entry,manifest],architecture_profile,limit=8)
        paths={hit.payload['file_path'] for hit in architecture_hits}
        self.assertIn('src/index.js',paths)
        self.assertIn('package.json',paths)

    def test_product_workflow_discovers_pascal_case_component_route_and_model(self):
        component=point('admin-ui','web/src/pages/AdminDashboard.jsx','AdminDashboard',
                        'const products = await axios.get(`${apiBase}/api/admin/products`);')
        route=point('admin-route','server/routes/admin.js','GET /products',
                    "router.get('/products', authenticateToken, listProducts)")
        product=point('product-model','server/models/Product.js','Product','const ProductSchema = new Schema({ title: String })')
        guard=point('admin-guard','web/src/components/Auth/AdminRoute.jsx','AdminRoute',
                    "if (user.role !== 'admin') return <Navigate to='/login' />")
        query=analyze_query('How are products created and managed by administrators from the frontend through the API and product model?')
        merged=merge_rankings([component],[component,route,product,guard],query)
        related=discover_import_related_hits([merged[0]],[component,route,product,guard],merged)
        roles=discover_source_role_hits(merged,query)
        paths={hit.payload['file_path'] for hit in prioritize_workflow_roles(roles+merged,query,max_hits=8)+related}
        self.assertTrue({'web/src/pages/AdminDashboard.jsx','server/routes/admin.js','server/models/Product.js'}.issubset(paths))
        self.assertNotIn('web/src/components/Auth/AdminRoute.jsx',paths)

    def test_template_api_calls_resolve_mounted_routes_and_route_imported_models(self):
        frontend=point('frontend','web/src/pages/AdminDashboard.jsx','AdminDashboard',
                       'await axios.get(`${apiBase}/api/admin/products`);')
        entry=point('entry','server/index.js','index',
                    "const adminRoutes = require('./routes/admin'); app.use('/api/admin', adminRoutes);")
        route=point('route','server/routes/admin.js','admin',
                    "const Product = require('../models/Product'); router.get('/products', listProducts);")
        route.payload['symbol_type']='route'
        model=point('model','server/models/Product.js','Product','const ProductSchema = new Schema({ title: String })')
        points=[frontend,entry,route,model]
        profile=analyze_query('Trace the product management workflow from the frontend to the backend route and model')
        ranked=merge_rankings([frontend],lexical_rank(points,profile),profile)
        mounted=discover_import_related_hits([merge_rankings([frontend],[],profile)[0]],points,ranked)
        # The template endpoint is linked through the mounted router; the second
        # graph step follows that route's local model import.
        self.assertIn('server/routes/admin.js',{hit.payload['file_path'] for hit in mounted})
        transitive=discover_import_related_hits(mounted,points,ranked)
        self.assertIn('server/models/Product.js',{hit.payload['file_path'] for hit in transitive})

    def test_end_to_end_order_workflow_context_keeps_checkout_and_backend_evidence(self):
        checkout=point('checkout','web/src/pages/Checkout.jsx','Checkout',
                       'async function handlePlaceOrder(){ await axios.post(`${apiBase}/api/orders`); await axios.post(`${apiBase}/api/payments/verify`) }')
        orders=point('orders','server/routes/orders.js','orders',"router.post('/', authenticateToken, createOrder)")
        payments=point('payments','server/routes/payments.js','payments',"router.post('/verify', authenticateToken, verifyPayment)")
        order_model=point('order-model','server/models/Order.js','Order','const OrderSchema = new Schema({ user: ObjectId, products: [] })')
        middleware=point('auth','server/middleware/auth.js','authenticateToken','function authenticateToken(req,res,next) { next() }')
        points=[checkout,orders,payments,order_model,middleware]
        query=analyze_query('Explain the complete order creation and payment verification workflow, including the frontend components, backend routes, database model and authentication middleware')
        hits=retrieve_results([orders,payments],points,query,limit=6)
        context=build_context(hits,token_budget=5000,max_lines_per_source=12,fair_by_file=True,profile=query)
        paths={source['file_path'] for source in context['sources']}
        self.assertTrue({'web/src/pages/Checkout.jsx','server/routes/orders.js','server/routes/payments.js','server/models/Order.js','server/middleware/auth.js'}.issubset(paths))

    def test_order_placement_prefers_posting_checkout_component_over_read_only_order_pages(self):
        checkout=point('checkout','web/src/pages/Checkout.jsx','Checkout',
                       "await axios.post(`${API_URL}/api/orders`, payload)")
        order_list=point('orders-ui','web/src/pages/Orders.jsx','Orders',
                         "const orders = await axios.get(`${API_URL}/api/orders`)")
        login=point('login','web/src/components/Auth/Login.jsx','Login',
                    'const { user } = useAuth(); await login(email, password)')
        query=analyze_query('Explain how a user places an order from the frontend and how the order is created in the backend, including authentication.')
        ranked=merge_rankings([order_list,login,checkout],[checkout,order_list,login],query)
        roles=discover_source_role_hits(ranked,query,128)
        selected=prioritize_workflow_roles(roles+ranked,query,max_hits=8)
        self.assertEqual(selected[0].payload['file_path'],'web/src/pages/Checkout.jsx')
        self.assertNotIn('web/src/components/Auth/Login.jsx',[hit.payload['file_path'] for hit in selected])

    def test_exact_checkout_order_payment_question_keeps_named_checkout_in_final_results(self):
        query=analyze_query('Explain the complete order placement and payment verification flow in this repository. Trace from the Checkout frontend component through backend order and payment routes to database models.')
        checkout=point('checkout','web/src/pages/Checkout.jsx','handlePlaceOrder',
                       "async function handlePlaceOrder(){ await axios.post('/api/orders') }",241,260)
        orders_ui=point('orders-ui','web/src/pages/Orders.jsx','initializeRazorpayPayment',
                        "const response = await axios.post('/api/payments/create-order'); loadRazorpayCheckout()",81,100)
        orders=point('orders','server/routes/orders.js','POST /api/orders',"router.post('/api/orders',createOrder)")
        payments=point('payments','server/routes/payments.js','POST /api/payments/verify',"router.post('/api/payments/verify',verifyPayment)")
        model=point('order-model','server/models/Order.js','Order','const OrderSchema = new Schema({ user: ObjectId })')
        points=[checkout,orders_ui,orders,payments,model]
        results=retrieve_results([orders_ui,payments],points,query,limit=6)
        paths={hit.payload['file_path'] for hit in results}
        self.assertIn('web/src/pages/Checkout.jsx',paths)
        self.assertIn('server/routes/orders.js',paths)
        self.assertIn('server/routes/payments.js',paths)
        self.assertIn('server/models/Order.js',paths)

    def test_business_workflow_keeps_mutation_chunk_from_multi_chunk_frontend_file(self):
        shell=point('checkout-shell','web/src/pages/Checkout.jsx','Checkout',
                    'export default function Checkout(){ return <main>Order checkout</main> }',1,100)
        submit=point('checkout-submit','web/src/pages/Checkout.jsx','handlePlaceOrder',
                     'async function handlePlaceOrder(){ await axios.post(`${API_URL}/api/orders`, payload) }',301,340)
        order_list=point('orders-ui','web/src/pages/Orders.jsx','Orders',
                         'const orders = await axios.get(`${API_URL}/api/orders`)',1,80)
        query=analyze_query('Explain how a user places an order from the frontend and how it is created by the backend.')
        ranked=merge_rankings([shell,order_list,submit],[shell,order_list,submit],query)
        roles=discover_source_role_hits(ranked,query,128)
        checkout_role=next(hit for hit in roles if hit.payload['file_path']=='web/src/pages/Checkout.jsx')
        self.assertEqual(checkout_role.id,'checkout-submit')

    def test_model_coverage_reserves_one_relevant_chunk_per_requested_model(self):
        query=analyze_query('Which database models represent users, products, orders, reviews and payments?')
        models=[point(str(i),f'server/models/{name}.js',name,f'class {name} {{}}')
                for i,name in enumerate(('User','Product','Order','Review','Payment','Category'))]
        ranked=merge_rankings(models,models,query)
        coverage=discover_model_coverage_hits(ranked,query,max_paths=8)
        paths={hit.payload['file_path'] for hit in coverage}
        self.assertTrue(all(f'server/models/{name}.js' in paths for name in ('User','Product','Order','Review','Payment')))
        self.assertNotIn('server/models/Category.js',paths)

    def test_architecture_evidence_covers_entrypoint_config_database_routes_and_services(self):
        hits=[merge_rankings([point('entry','src/main.js','main','bootstrap()')],[],analyze_query('architecture entrypoint configuration database routes services'))[0],
              merge_rankings([point('config','package.json','package','{}')],[],analyze_query('architecture entrypoint configuration database routes services'))[0],
              merge_rankings([point('db','src/db.py','connect','create_engine(DATABASE_URL)')],[],analyze_query('architecture entrypoint configuration database routes services'))[0],
              merge_rankings([point('route','src/routes/users.py','route','GET /users')],[],analyze_query('architecture entrypoint configuration database routes services'))[0],
              merge_rankings([point('service','src/services/users.py','service','def load_users(): pass')],[],analyze_query('architecture entrypoint configuration database routes services'))[0]]
        paths={hit.payload['file_path'] for hit in discover_architecture_hits(hits,8)}
        self.assertEqual(paths,{'src/main.js','package.json','src/db.py','src/routes/users.py','src/services/users.py'})

    def test_dense_and_lexical_merge_uses_rrf_and_retains_metadata(self):
        a=point('a','src/a.py','verifyPayment','def verifyPayment(): pass')
        b=point('b','src/b.py','saveOrder','def saveOrder(): pass')
        profile=analyze_query('verifyPayment')
        hits=merge_rankings([b,a],[a,b],profile)
        self.assertEqual(hits[0].id,'a')
        self.assertEqual(hits[0].payload['file_path'],'src/a.py')
        self.assertGreater(hits[0].score,0)

    def test_duplicate_elimination_overlap_suppression_and_file_diversity(self):
        profile=analyze_query('explain the complete order workflow across multiple files')
        a=point('a','a.py','one','same',1,20,digest='same')
        duplicate=point('a2','a.py','one','same',1,20,digest='same')
        overlap=point('a3','a.py','two','overlap',2,18,digest='different')
        b=point('b','b.py','three','other',1,10)
        selected=select_diverse(merge_rankings([a,duplicate,overlap,b],[],profile),profile,8)
        self.assertEqual([x.id for x in selected],['a','b'])

    def test_identical_content_in_distinct_paths_keeps_both_citable_files(self):
        profile=analyze_query('explain complete workflow across multiple files')
        a=point('a','frontend/a.js','same','shared implementation',digest='identical')
        b=point('b','backend/b.js','same','shared implementation',digest='identical')
        selected=select_diverse(merge_rankings([a,b],[],profile),profile,8)
        self.assertEqual({hit.payload['file_path'] for hit in selected},{'frontend/a.js','backend/b.js'})

    def test_repository_and_generation_isolation(self):
        valid=point('a','a.py','a','x',repo=1,generation='new')
        other_repo=point('b','b.py','b','x',repo=2,generation='new')
        old_gen=point('c','c.py','c','x',repo=1,generation='old')
        self.assertEqual([p.id for p in filter_repository_points([valid,other_repo,old_gen],1,'new')],['a'])
        self.assertEqual(filter_repository_points([valid,other_repo,old_gen],1,None),[])

    def test_scroll_follows_all_pages(self):
        pages={None:([1,2],'p2'),'p2':([3,4],'p3'),'p3':([5],None)}
        result=scroll_all_pages(lambda offset:pages[offset],page_size=2)
        self.assertEqual(result,[1,2,3,4,5])

    def test_context_budget_and_citation_metadata(self):
        profile=analyze_query('explain these functions')
        points=[point(str(i),f'src/{i}.py',f'symbol{i}','x'*1200,start=i*10+1,end=i*10+3) for i in range(20)]
        hits=merge_rankings(points,points,profile)
        context=build_context(retrieve_results(hits,points,profile,limit=8),token_budget=700)
        self.assertLessEqual(context['estimated_tokens'],700)
        self.assertTrue(context['sources'])
        self.assertEqual(context['sources'][0]['citation_id'],'S1')
        self.assertTrue(context['sources'][0]['file_path'])
        self.assertTrue(context['sources'][0]['start_line'])

    def test_authentication_workflow_retrieval_reserves_related_indexed_files(self):
        paths=[
            'README.md',
            'craft-app-backend/routes/auth.js',
            'craft-app-backend/middleware/auth.js',
            'craft-app-backend/utils/jwt.js',
            'craft-app-frontend/src/context/AuthContext.jsx',
            'craft-app-frontend/src/components/Auth/Login.jsx',
            'craft-app-frontend/src/components/Auth/Register.jsx',
            'craft-app-frontend/src/components/Auth/PrivateRoute.jsx',
            'craft-app-frontend/src/components/Auth/AdminRoute.jsx',
            'craft-app-frontend/src/components/Auth/CreatorRoute.jsx',
            'craft-app-backend/models/User.js',
            'craft-app-backend/utils/sendEmail.js',
            'craft-app-frontend/src/pages/VerifyEmail.jsx',
        ]
        points=[point(p,p,p.rsplit('/',1)[-1],('authentication login register jwt route token ' if p!='README.md' else 'authentication workflow login register jwt route ')+p) for p in paths]
        profile=analyze_query('Explain the complete authentication flow from login to JWT and protected frontend routes')
        results=retrieve_results([points[0]],points,profile,limit=8)
        retrieved={hit.payload['file_path'] for hit in results}
        self.assertTrue(set(paths[1:]).issubset(retrieved))
        self.assertNotIn('README.md', [hit.payload['file_path'] for hit in results[:9]])

    def test_context_splits_auth_citations_to_narrow_source_ranges(self):
        source='\n'.join(f'line_{i}' for i in range(1, 41))
        hit=merge_rankings([point('auth','middleware/auth.js','authenticate',source,20,59)],[],analyze_query('authentication'))[0]
        context=build_context([hit],token_budget=2000,max_lines_per_source=12)
        self.assertGreater(len(context['sources']),1)
        self.assertTrue(all(s['end_line']-s['start_line'] < 12 for s in context['sources']))
        self.assertTrue(all(s['source_kind']=='source_code' for s in context['sources']))
        self.assertEqual((context['sources'][0]['start_line'],context['sources'][0]['end_line']),(20,31))

    def test_context_round_robins_auth_files_before_adding_more_from_one_large_file(self):
        profile=analyze_query('Explain the complete authentication workflow across backend and frontend')
        hits=[merge_rankings([point('large','routes/auth.js','auth','\n'.join(f'auth_line_{i}' for i in range(1,121)),1,120)],[],profile)[0]]
        for i,path in enumerate(('middleware/auth.js','utils/jwt.js','components/Auth/Register.jsx','components/Auth/PrivateRoute.jsx')):
            hits.append(merge_rankings([point(str(i),path,path,'one relevant auth line',1,1)],[],profile)[0])
        context=build_context(hits,token_budget=600,max_lines_per_source=12,fair_by_file=True)
        cited_paths={source['file_path'] for source in context['sources']}
        self.assertTrue({hit.payload['file_path'] for hit in hits}.issubset(cited_paths))
        self.assertLessEqual(context['estimated_tokens'],600)

    def test_context_prioritizes_query_relevant_later_windows_for_any_file(self):
        lines=[f'unrelated helper statement {i}' for i in range(1,49)]
        lines[35]='async function handlePlaceOrder(){ return api.post("/orders") }'
        source=point('checkout','src/Checkout.jsx','Checkout','\n'.join(lines),1,48)
        profile=analyze_query('Explain handlePlaceOrder')
        hit=merge_rankings([source],[],profile)[0]
        context=build_context([hit],token_budget=1200,max_lines_per_source=12,
                              fair_by_file=True,profile=profile)
        self.assertIn('handlePlaceOrder',context['context'])
        matching=next(s for s in context['sources'] if 'handlePlaceOrder' in context['context'].split(f"[{s['citation_id']}]")[1].split('```')[1])
        self.assertGreater(matching['start_line'],1)

    def test_workflow_retrieval_discovers_imported_modules_and_called_api_routes(self):
        checkout=point('checkout','web/src/pages/Checkout.jsx','Checkout',
                       "import api from '../services/api'; async function handlePlaceOrder(){ return api.post('/api/orders') }")
        api=point('api','web/src/services/api.js','api','export default axios.create({ baseURL: "/api" })')
        route=point('orders','server/routes/orders.js','POST /api/orders',
                    "router.post('/api/orders', auth, async (req,res)=> res.json({id:1}))")
        model=point('model','server/models/Order.js','Order','class Order { save() {} }')
        points=[checkout,api,route,model]
        profile=analyze_query('Find handlePlaceOrder and explain the API calls it makes across frontend and backend')
        ranked=merge_rankings([checkout],lexical_rank(points,profile),profile)
        related=discover_import_related_hits([merge_rankings([checkout],[],profile)[0]],points,ranked)
        related_paths={hit.payload['file_path'] for hit in related}
        self.assertIn('web/src/services/api.js',related_paths)
        self.assertIn('server/routes/orders.js',related_paths)

    def test_route_import_discovery_finds_middleware_models_and_utility_across_directories(self):
        route=point('route','server/routes/orders.js','orders',
                    "import auth from '../middleware/auth'; import Order from '../models/Order'; import {makeId} from '../utils/id'; router.post('/orders',auth,save)")
        middleware=point('middleware','server/middleware/auth.js','auth','export default function auth(req,res,next){ next() }')
        model=point('model','server/models/Order.js','Order','class Order { save(){} }')
        utility=point('utility','server/utils/id.js','makeId','export function makeId(){ return crypto.randomUUID() }')
        points=[route,middleware,model,utility]
        profile=analyze_query('Trace order creation across routes and supporting files')
        ranked=merge_rankings([route],lexical_rank(points,profile),profile)
        related=discover_import_related_hits([merge_rankings([route],[],profile)[0]],points,ranked)
        paths={hit.payload['file_path'] for hit in related}
        self.assertTrue({'server/middleware/auth.js','server/models/Order.js','server/utils/id.js'}.issubset(paths))

    def test_business_and_database_workflows_reserve_related_source_roles(self):
        admin=point('admin','web/src/AdminDashboard.jsx','AdminDashboard','create product with api.post("/api/products")')
        route=point('route','server/routes/products.js','POST /api/products','router.post("/api/products", createProduct)')
        route.payload['symbol_type']='route'
        model=point('model','server/models/Product.js','Product','const ProductSchema = new Schema({ category: String })')
        model.payload['symbol_type']='model'
        distractor=point('distractor','server/routes/orders.js','orders','router.get("/orders")')
        workflow=analyze_query('How are products created and managed by administrators from the frontend through the API and product model?')
        hits=retrieve_results([admin,distractor],[admin,route,model,distractor],workflow,limit=8)
        paths={hit.payload['file_path'] for hit in hits}
        self.assertTrue({'web/src/AdminDashboard.jsx','server/routes/products.js','server/models/Product.js'}.issubset(paths))

        db_profile=analyze_query('Which database models represent users, products, orders, reviews and payments?')
        model_hits=[point(str(i),f'server/models/{name}.js',name,f'{name} schema references user product order payment review')
                    for i,name in enumerate(('User','Product','Order','Review','Payment'))]
        route_hit=point('admin-route','server/routes/admin.js','admin','admin management')
        database_results=retrieve_results([route_hit],model_hits+[route_hit],db_profile,limit=8)
        self.assertTrue(all(any(h.payload['file_path'].endswith(f'/models/{name}.js') for h in database_results)
                            for name in ('User','Product','Order','Review','Payment')))

    def test_named_frontend_api_call_can_discover_backend_route(self):
        checkout=point('checkout','web/src/Checkout.jsx','handlePlaceOrder',
                       "async function handlePlaceOrder(){ await api.post('/api/orders') }")
        route=point('orders','server/routes/orders.js','POST /orders',
                    "router.post('/orders', auth, createOrder)")
        route.payload['symbol_type']='route'
        profile=analyze_query('Find handlePlaceOrder and explain the API calls it makes.')
        results=retrieve_results([checkout],[checkout,route],profile,limit=8)
        self.assertTrue(any(hit.payload['file_path']=='server/routes/orders.js' for hit in results))

    def test_auth_context_prioritizes_later_verification_windows_without_starving_other_files(self):
        lines=[f'// unrelated helper line {i}' for i in range(1, 49)]
        lines[35:39]=['router.get("/verify-email/:token", verifyEmail)',
                      'const user = await User.findOne({ verificationToken: token })',
                      'user.isActive = true', 'await user.save()']
        auth=point('auth','craft-app-backend/routes/auth.js','auth routes','\n'.join(lines),1,48)
        register=point('register','craft-app-frontend/src/components/Auth/Register.jsx',
                       'Register','function Register(){ return <form onSubmit={registerUser} /> }',1,1)
        context=build_context([merge_rankings([auth,register],[],analyze_query('Trace the complete authentication flow'))[0],
                               merge_rankings([register,auth],[],analyze_query('Trace the complete authentication flow'))[0]],
                              token_budget=1000,max_lines_per_source=12,fair_by_file=True,
                              prioritize_auth_workflow=True)
        self.assertIn('verify-email',context['context'])
        self.assertIn('Register.jsx',context['context'])
        verify_source=next(s for s in context['sources'] if s['file_path'].endswith('routes/auth.js'))
        self.assertLessEqual(verify_source['end_line']-verify_source['start_line']+1,12)

    def test_auth_inventory_identifies_email_verification_and_account_files(self):
        inventory=authentication_workflow_index_inventory([
            'craft-app-backend/models/User.js','craft-app-backend/utils/sendEmail.js',
            'craft-app-frontend/src/pages/VerifyEmail.jsx'])
        self.assertEqual(inventory['account_model'],['craft-app-backend/models/User.js'])
        self.assertEqual(inventory['email_sender'],['craft-app-backend/utils/sendEmail.js'])
        self.assertEqual(inventory['email_verification_page'],['craft-app-frontend/src/pages/VerifyEmail.jsx'])

    def test_focused_registration_and_verification_select_only_the_requested_evidence_stages(self):
        paths=['craft-app-backend/routes/auth.js','craft-app-backend/models/User.js',
               'craft-app-frontend/src/components/Auth/Register.jsx',
               'craft-app-frontend/src/pages/VerifyEmail.jsx']
        sources=[{'citation_id':f'S{i+1}','file_path':path,'source_kind':'source_code'} for i,path in enumerate(paths)]
        stages,unavailable=authentication_workflow_stage_requirements(
            'Explain user registration and email verification.',sources)
        self.assertEqual(set(stages),{'registration','email verification and activation'})
        self.assertEqual(unavailable,[])
        self.assertEqual(len(stages['registration']),2)
        self.assertEqual(len(stages['email verification and activation']),3)
        focused_flow,_=authentication_workflow_stage_requirements(
            'Explain the complete user registration and email verification flow.',sources)
        self.assertEqual(set(focused_flow),{'registration','email verification and activation'})

    def test_order_payment_flow_does_not_activate_authentication_unavailable_stages(self):
        question=('Explain the complete order placement and payment verification flow. Trace from the Checkout '
                  'frontend component through the backend order and payment routes to the database models.')
        context=[
            {'citation_id':'S1','file_path':'web/src/pages/Checkout.jsx','source_kind':'source_code'},
            {'citation_id':'S2','file_path':'server/routes/orders.js','source_kind':'source_code'},
            {'citation_id':'S3','file_path':'server/routes/payments.js','source_kind':'source_code'},
            {'citation_id':'S4','file_path':'server/models/Order.js','source_kind':'source_code'},
            {'citation_id':'S5','file_path':'server/middleware/auth.js','source_kind':'source_code'},
        ]
        auth_stages,auth_unavailable=authentication_workflow_stage_requirements(question,context)
        business_stages,business_unavailable=business_workflow_stage_requirements(question,context)
        self.assertEqual(auth_stages,{})
        self.assertEqual(auth_unavailable,[])
        self.assertEqual(set(business_stages),{'frontend order placement','backend order route',
                                              'backend payment verification','order database model'})
        self.assertEqual(business_unavailable,[])

    def test_named_checkout_missing_from_context_is_reported_only_for_that_stage(self):
        question='Trace the order placement and payment verification flow from the Checkout frontend through backend routes to the database model.'
        context=[{'citation_id':'S1','file_path':'web/src/pages/Orders.jsx','source_kind':'source_code'},
                 {'citation_id':'S2','file_path':'server/routes/orders.js','source_kind':'source_code'},
                 {'citation_id':'S3','file_path':'server/routes/payments.js','source_kind':'source_code'},
                 {'citation_id':'S4','file_path':'server/models/Order.js','source_kind':'source_code'}]
        stages,unavailable=business_workflow_stage_requirements(question,context)
        self.assertNotIn('frontend order placement',stages)
        self.assertEqual(unavailable,['frontend order placement'])

    def test_index_inventory_distinguishes_paths_missing_from_index(self):
        inventory=authentication_workflow_index_inventory(['middleware/auth.js','utils/jwt.js'])
        text=format_authentication_workflow_inventory(inventory)
        self.assertIn('middleware/auth.js',text)
        self.assertIn('no matching path found in the indexed repository',text)
        notes=missing_authentication_workflow_evidence(inventory,[{'file_path':'middleware/auth.js'}])
        self.assertTrue(any('indexed jwt/token utility path(s) exist but were not retrieved' in note.lower() for note in notes))
        self.assertTrue(any('No indexed path matching the expected frontend auth context/provider role' in note for note in notes))

if __name__ == '__main__': unittest.main()
